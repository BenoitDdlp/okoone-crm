"""Background research loop — runs continuously, Karpathy-style.

Architecture: Claude = brain, Patchright = hands.
- Claude generates search queries from the program (informed by past query performance)
- Patchright scrapes LinkedIn
- Claude evaluates prospects
- Metrics computed per cycle: qualification_rate, novelty_rate, diversity_score, avg_score
- Claude proposes program improvements (with metrics trends from last 5 cycles)
- Auto-accept if configured, otherwise pending human review
- Failure detection: alerts when scraping yields 0 results
"""

import json
import logging
import traceback
from datetime import datetime

from app.config import settings
from app.database import get_db
from app.scraper.rate_limiter import DailyLimitReached
from app.services.autoresearch_service import AutoresearchService

logger = logging.getLogger("okoone.loop")

# Global state visible from the UI (polled every 5s)
LOOP_STATE: dict = {
    "active": False,
    "status": "idle",
    "last_run_at": None,
    "last_run_result": None,
    "current_step": None,
    "cycles_completed": 0,
    "total_prospects_found": 0,
    "total_qualified": 0,
    "last_error": None,
    "consecutive_empty_runs": 0,
}

# Scraper instance — set by main.py lifespan via set_scraper()
_scraper = None
_rate_limiter = None


def set_scraper(scraper, rate_limiter) -> None:
    global _scraper, _rate_limiter
    _scraper = scraper
    _rate_limiter = rate_limiter


async def run_research_loop() -> None:
    """One cycle of the research loop. Called by APScheduler every N minutes."""
    if not LOOP_STATE["active"]:
        return

    if not _scraper:
        LOOP_STATE["status"] = "error"
        LOOP_STATE["last_error"] = "Scraper non initialise"
        return

    research = AutoresearchService()

    try:
        # --- Step 1: Generate search queries ---
        LOOP_STATE["status"] = "searching"
        LOOP_STATE["current_step"] = "Claude genere des requetes de recherche..."

        async with get_db() as db:
            queries = await research.generate_search_plan(db)
            if not queries:
                LOOP_STATE["current_step"] = "Claude n'a genere aucune query."
                LOOP_STATE["status"] = "sleeping"
                return

            # --- Step 2: Start browser if needed ---
            if not _scraper._browser:
                LOOP_STATE["current_step"] = "Demarrage du navigateur..."
                await _scraper.start()

            # --- Step 3: Check LinkedIn session ---
            session_valid = await _scraper.is_session_valid()
            if not session_valid:
                LOOP_STATE["last_error"] = "Session LinkedIn expiree — scraping skipped, analyse continue"
                logger.warning("LinkedIn session expired — skipping scrape, continuing with analysis/improvement")
                # DON'T stop the loop — still do analysis + improvement steps
                # The loop NEVER STOPS (Karpathy: "do NOT pause to ask the human")

            # --- Step 4: Scrape LinkedIn (only if session valid) ---
            total_found = 0
            total_new = 0
            from app.repositories.prospect_repo import ProspectRepository

            repo = ProspectRepository(db)
            query_prospect_map: dict[str, list[int]] = {}

            if not session_valid:
                logger.info("SCRAPE SKIPPED (session expired) — jumping to analysis steps")

            if session_valid:
                for i, q in enumerate(queries[:5]):
                    kw = q["keywords"]
                loc = q.get("location")
                query_key = f"{kw}||{loc or ''}"
                query_prospect_map[query_key] = []
                logger.info("SCRAPE [%d/%d] keywords='%s' location='%s'", i + 1, min(len(queries), 5), kw, loc)
                LOOP_STATE["current_step"] = f"Recherche {i+1}/{min(len(queries), 5)}: {kw[:40]}..."

                try:
                    results = await _scraper.search_people(kw, loc)
                    logger.info("SCRAPE [%d/%d] got %d results", i + 1, min(len(queries), 5), len(results))
                    for j, r in enumerate(results[:3]):
                        logger.info("  result[%d]: %s", j, {k: str(v)[:50] for k, v in r.items()})
                    total_found += len(results)

                    cursor = await db.execute(
                        "INSERT INTO search_queries (keywords, location) VALUES (?, ?)",
                        (kw, loc),
                    )
                    await db.commit()

                    for p in results:
                        username = (p.get("profile_username") or "").strip("/").split("/")[-1]
                        if not username or len(username) < 2:
                            logger.debug("  skipping result with no username: %s", p.get("full_name"))
                            continue
                        pid, is_new = await repo.upsert_by_username(username, {
                            "full_name": p.get("full_name", ""),
                            "headline": p.get("headline", ""),
                            "location": p.get("location", ""),
                            "linkedin_url": f"https://www.linkedin.com/in/{username}/",
                            "linkedin_username": username,
                            "source_search_id": cursor.lastrowid,
                            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        if is_new:
                            total_new += 1
                            query_prospect_map[query_key].append(pid)
                            logger.info("  NEW prospect: %s (%s)", p.get("full_name"), username)
                    await db.commit()

                except DailyLimitReached as e:
                    LOOP_STATE["status"] = "rate_limited"
                    LOOP_STATE["current_step"] = "Rate limit LinkedIn atteint. Reprise demain."
                    logger.warning("RATE LIMIT: %s", e)
                    break
                except Exception:
                    logger.error("SCRAPE ERROR for '%s':", kw, exc_info=True)

            # --- Step 4.5: Deep screening (fetch full profiles) ---
            LOOP_STATE["status"] = "evaluating"
            deep_screened = 0
            # Get prospects that have no experience data yet
            shallow_cursor = await db.execute(
                "SELECT id, linkedin_username FROM prospects "
                "WHERE (experience_json IS NULL OR experience_json = '' OR experience_json = '[]') "
                "AND linkedin_username IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 10"
            )
            shallow_prospects = await shallow_cursor.fetchall()
            logger.info("DEEP SCREEN: %d prospects need full profile fetch", len(shallow_prospects))

            for i, sp in enumerate(shallow_prospects):
                pid, username = sp[0], sp[1]
                LOOP_STATE["current_step"] = f"Deep screening {i+1}/{len(shallow_prospects)}: {username}..."
                try:
                    profile = await _scraper.get_person_profile(username)
                    if profile:
                        update_data = {}
                        if profile.get("full_name"):
                            update_data["full_name"] = profile["full_name"]
                        if profile.get("headline"):
                            update_data["headline"] = profile["headline"]
                        if profile.get("location"):
                            update_data["location"] = profile["location"]
                        if profile.get("about"):
                            update_data["about_text"] = profile["about"]
                        if profile.get("current_company"):
                            update_data["current_company"] = profile["current_company"]
                        if profile.get("current_title"):
                            update_data["current_title"] = profile["current_title"]
                        if profile.get("experience"):
                            update_data["experience_json"] = json.dumps(profile["experience"])
                        if profile.get("education"):
                            update_data["education_json"] = json.dumps(profile["education"])
                        if profile.get("skills"):
                            update_data["skills_json"] = json.dumps(profile["skills"])
                        if profile.get("profile_photo_url"):
                            update_data["profile_photo_url"] = profile["profile_photo_url"]
                        update_data["screened_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                        if update_data:
                            await repo.update(pid, update_data)
                            deep_screened += 1
                            logger.info("DEEP SCREEN [%d] %s → %s @ %s",
                                        pid, profile.get("full_name", "?"),
                                        profile.get("current_title", "?"),
                                        profile.get("current_company", "?"))
                except DailyLimitReached:
                    logger.warning("DEEP SCREEN rate limit reached after %d profiles", deep_screened)
                    break
                except Exception:
                    logger.error("DEEP SCREEN ERROR for %s:", username, exc_info=True)
            await db.commit()
            logger.info("DEEP SCREEN complete: %d profiles enriched", deep_screened)

            # --- Step 5: Score new prospects ---
            LOOP_STATE["current_step"] = f"{total_new} nouveaux + {deep_screened} enrichis. Scoring..."

            scored = 0
            cursor = await db.execute(
                "SELECT id FROM prospects WHERE relevance_score = 0 OR relevance_score IS NULL"
            )
            unscored = [row[0] for row in await cursor.fetchall()]

            w_cursor = await db.execute(
                "SELECT criteria_json FROM scoring_weights WHERE is_active = 1 LIMIT 1"
            )
            w_row = await w_cursor.fetchone()
            if w_row and unscored:
                from app.services.scoring_service import ScoringService

                scoring = ScoringService()
                weights = json.loads(w_row[0])
                for pid in unscored:
                    prospect = await repo.find_by_id(pid)
                    if prospect:
                        score, breakdown = await scoring.score_prospect(prospect, weights)
                        summary = scoring.generate_score_summary(breakdown, weights, prospect)
                        await repo.update(pid, {
                            "relevance_score": score,
                            "score_breakdown": json.dumps(breakdown),
                            "score_summary": summary,
                            "status": "screened",
                        })
                        scored += 1
                await db.commit()

            # --- Step 5.5: Claude deep analysis on enriched prospects ---
            LOOP_STATE["status"] = "analyzing"
            LOOP_STATE["current_step"] = "Claude analyse en profondeur les prospects enrichis..."
            analyzed = 0
            try:
                from app.services.deep_analysis_service import DeepAnalysisService
                from app.services.autoresearch_service import AutoresearchService as _AR

                deep_svc = DeepAnalysisService()
                _ar = _AR()
                _prog_version, _prog_content = await _ar._load_program(db)
                _acqs = await _ar._load_acquaintances(db)

                # Prospects with experience data but no Claude analysis yet (max 5 per cycle)
                da_cursor = await db.execute(
                    "SELECT id FROM prospects "
                    "WHERE experience_json IS NOT NULL AND experience_json != '' AND experience_json != '[]' "
                    "AND (claude_analysis IS NULL OR claude_analysis = '') "
                    "ORDER BY relevance_score DESC LIMIT 5"
                )
                da_ids = [row[0] for row in await da_cursor.fetchall()]

                for i, pid in enumerate(da_ids):
                    LOOP_STATE["current_step"] = f"Analyse Claude {i+1}/{len(da_ids)}..."
                    prospect = await repo.find_by_id(pid)
                    if not prospect:
                        continue
                    try:
                        analysis = await deep_svc.analyze_prospect(prospect, _prog_content, _acqs)
                        await repo.update(pid, {"claude_analysis": json.dumps(analysis, ensure_ascii=False)})
                        analyzed += 1
                        logger.info(
                            "DEEP ANALYSIS [%d] %s → verdict=%s score=%s",
                            pid, prospect.get("full_name", "?"),
                            analysis.get("verdict"), analysis.get("score"),
                        )
                    except Exception:
                        logger.error("DEEP ANALYSIS ERROR for prospect %d:", pid, exc_info=True)
                await db.commit()
            except Exception:
                logger.error("DEEP ANALYSIS step failed:", exc_info=True)
            logger.info("DEEP ANALYSIS complete: %d prospects analyzed", analyzed)

            # --- Step 6: Compute cycle metrics ---
            LOOP_STATE["current_step"] = "Calcul des metriques du cycle..."

            all_new_ids: list[int] = []
            for ids in query_prospect_map.values():
                all_new_ids.extend(ids)

            # Build evaluations list from scored prospects for metrics
            evaluations: list[dict] = []
            if all_new_ids:
                placeholders = ",".join("?" * len(all_new_ids))
                eval_cursor = await db.execute(
                    f"SELECT id, relevance_score FROM prospects WHERE id IN ({placeholders})",
                    all_new_ids,
                )
                for row in await eval_cursor.fetchall():
                    score_val = row[1] or 0
                    evaluations.append({
                        "prospect_id": row[0],
                        "score": score_val,
                        "verdict": "qualified" if score_val > 50 else "maybe" if score_val > 30 else "reject",
                    })

            metrics = await research.compute_cycle_metrics(db, all_new_ids, evaluations)
            logger.info(
                "CYCLE METRICS: qual=%.1f%% novelty=%.1f%% diversity=%d avg=%.1f found=%d qualified=%d",
                metrics["qualification_rate"], metrics["novelty_rate"],
                metrics["diversity_score"], metrics["avg_score"],
                metrics["total_found"], metrics["total_qualified"],
            )

            # --- Step 6.5: Record run + query performance ---
            v_cursor = await db.execute(
                "SELECT version FROM prospect_program WHERE status = 'active' ORDER BY version DESC LIMIT 1"
            )
            v = await v_cursor.fetchone()
            program_version = v[0] if v else 0

            await db.execute("""
                INSERT INTO research_runs
                    (program_version, finished_at, status, prospects_found, prospects_qualified,
                     metric_json)
                VALUES (?, datetime('now'), ?, ?, ?, ?)
            """, (
                program_version,
                "completed" if total_new > 0 else "no_results",
                total_new,
                scored,
                json.dumps(metrics),
            ))
            await db.commit()

            run_id_cursor = await db.execute("SELECT last_insert_rowid()")
            run_id_row = await run_id_cursor.fetchone()
            current_run_id = run_id_row[0] if run_id_row else 0

            # Record per-query performance
            for query_key, prospect_ids in query_prospect_map.items():
                parts = query_key.split("||", 1)
                kw = parts[0]
                loc = parts[1] if len(parts) > 1 and parts[1] else None
                try:
                    await research.record_query_performance(db, kw, loc, current_run_id, prospect_ids)
                except Exception:
                    logger.error("Failed to record query performance for '%s':", kw, exc_info=True)
            await db.commit()

            # --- Step 7: Claude proposes improvements ---
            LOOP_STATE["status"] = "proposing"
            LOOP_STATE["current_step"] = "Claude analyse les metriques et propose des ameliorations..."
            try:
                proposal = await research.propose_program_improvement(db)
            except Exception as e:
                logger.warning("Program improvement proposal failed: %s", str(e)[:100])
                proposal = {}

            # Update run record with proposal
            if proposal:
                await db.execute("""
                    UPDATE research_runs SET
                        proposed_program = ?,
                        proposal_reasoning = ?
                    WHERE id = ?
                """, (
                    proposal.get("proposed_program", ""),
                    (proposal.get("analysis") or "")[:2000],
                    current_run_id,
                ))
                await db.commit()

            # --- Step 7.5: Auto-accept improvements if configured ---
            if settings.AUTO_ACCEPT_IMPROVEMENTS and proposal.get("proposed_program"):
                try:
                    new_version = await research.apply_program(
                        db, proposal["proposed_program"], author="claude-auto"
                    )
                    await db.execute(
                        "UPDATE research_runs SET proposal_status = 'auto-accepted' WHERE id = ?",
                        (current_run_id,),
                    )
                    await db.commit()
                    logger.info("AUTO-ACCEPTED program v%d from run %d", new_version, current_run_id)
                except Exception:
                    logger.error("Failed to auto-accept program improvement:", exc_info=True)

        # --- Step 8: Update state + failure detection ---
        LOOP_STATE["status"] = "sleeping"
        LOOP_STATE["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        LOOP_STATE["cycles_completed"] += 1
        LOOP_STATE["total_prospects_found"] += total_new
        LOOP_STATE["total_qualified"] += scored
        LOOP_STATE["last_error"] = None

        if total_new == 0:
            LOOP_STATE["consecutive_empty_runs"] += 1
            if LOOP_STATE["consecutive_empty_runs"] >= 3:
                LOOP_STATE["current_step"] = (
                    f"Attention : {LOOP_STATE['consecutive_empty_runs']} cycles consecutifs sans nouveau prospect. "
                    "Le programme ou les queries doivent etre ajustes."
                )
            else:
                LOOP_STATE["current_step"] = f"Cycle termine. 0 nouveaux prospects (#{LOOP_STATE['consecutive_empty_runs']} consecutif)."
        else:
            LOOP_STATE["consecutive_empty_runs"] = 0
            LOOP_STATE["current_step"] = (
                f"Cycle termine. +{total_new} prospects, {scored} scores. "
                f"Metriques: qual={metrics['qualification_rate']:.0f}% "
                f"novelty={metrics['novelty_rate']:.0f}% "
                f"diversity={metrics['diversity_score']} "
                f"avg={metrics['avg_score']:.0f}"
            )

        LOOP_STATE["last_run_result"] = {
            "queries": min(len(queries), 5),
            "found": total_found,
            "new": total_new,
            "scored": scored,
            "metrics": metrics,
        }

    except Exception as e:
        LOOP_STATE["status"] = "error"
        LOOP_STATE["last_error"] = str(e)[:200]
        LOOP_STATE["current_step"] = f"Erreur: {str(e)[:100]}"
        logger.error("Research loop error: %s", traceback.format_exc())
