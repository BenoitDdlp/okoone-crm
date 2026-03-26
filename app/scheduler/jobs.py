"""Background research loop — runs continuously, Karpathy-style.

Architecture: Claude = brain, Patchright = hands.
- Claude generates search queries from the program
- Patchright scrapes LinkedIn
- Claude evaluates prospects
- Claude proposes program improvements
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
            if not await _scraper.is_session_valid():
                LOOP_STATE["status"] = "session_expired"
                LOOP_STATE["current_step"] = "Session LinkedIn expiree. Reconnexion necessaire."
                LOOP_STATE["last_error"] = "Session LinkedIn expiree"
                LOOP_STATE["active"] = False  # Stop the loop
                return

            # --- Step 4: Scrape LinkedIn ---
            total_found = 0
            total_new = 0
            from app.repositories.prospect_repo import ProspectRepository

            repo = ProspectRepository(db)

            for i, q in enumerate(queries[:5]):
                kw = q["keywords"]
                loc = q.get("location")
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
                        _, is_new = await repo.upsert_by_username(username, {
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
                        summary = scoring.generate_score_summary(breakdown, weights)
                        await repo.update(pid, {
                            "relevance_score": score,
                            "score_breakdown": json.dumps(breakdown),
                            "score_summary": summary,
                            "status": "screened",
                        })
                        scored += 1
                await db.commit()

            # --- Step 6: Claude proposes improvements ---
            LOOP_STATE["status"] = "proposing"
            LOOP_STATE["current_step"] = "Claude analyse et propose des ameliorations..."
            try:
                proposal = await research.propose_program_improvement(db)
            except Exception as e:
                logger.warning("Program improvement proposal failed: %s", str(e)[:100])
                proposal = {}

            # --- Step 7: Record run ---
            v_cursor = await db.execute(
                "SELECT version FROM prospect_program WHERE status = 'active' ORDER BY version DESC LIMIT 1"
            )
            v = await v_cursor.fetchone()
            await db.execute("""
                INSERT INTO research_runs
                    (program_version, finished_at, status, prospects_found, prospects_qualified,
                     proposed_program, proposal_reasoning)
                VALUES (?, datetime('now'), ?, ?, ?, ?, ?)
            """, (
                v[0] if v else 0,
                "completed" if total_new > 0 else "no_results",
                total_new,
                scored,
                proposal.get("proposed_program", ""),
                (proposal.get("analysis") or "")[:2000],
            ))
            await db.commit()

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
            LOOP_STATE["current_step"] = f"Cycle termine. +{total_new} prospects, {scored} scores. Prochain cycle en attente."

        LOOP_STATE["last_run_result"] = {
            "queries": min(len(queries), 5),
            "found": total_found,
            "new": total_new,
            "scored": scored,
        }

    except Exception as e:
        LOOP_STATE["status"] = "error"
        LOOP_STATE["last_error"] = str(e)[:200]
        LOOP_STATE["current_step"] = f"Erreur: {str(e)[:100]}"
        logger.error("Research loop error: %s", traceback.format_exc())
