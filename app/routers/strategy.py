import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.services.autoresearch_service import AutoresearchService

router = APIRouter(tags=["strategy"])
templates = Jinja2Templates(directory="templates")
research = AutoresearchService()


def _get_loop_state(request: Request) -> dict:
    return getattr(request.app.state, "loop_state", {
        "active": False, "status": "idle", "current_step": None,
        "last_run_at": None, "cycles_completed": 0,
        "total_prospects_found": 0, "total_qualified": 0, "last_error": None,
    })


@router.get("/strategy", response_class=HTMLResponse)
async def strategy_page(request: Request):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT version, content FROM prospect_program WHERE status = 'active' ORDER BY version DESC LIMIT 1"
        )
        program = await cursor.fetchone()

        cursor = await db.execute("SELECT * FROM prospect_program ORDER BY version DESC LIMIT 20")
        versions = [dict(r) for r in await cursor.fetchall()]

        cursor = await db.execute("SELECT * FROM acquaintances ORDER BY created_at DESC")
        acquaintances = [dict(r) for r in await cursor.fetchall()]

        cursor = await db.execute("SELECT * FROM research_runs ORDER BY started_at DESC LIMIT 10")
        runs = [dict(r) for r in await cursor.fetchall()]

        # Check if we have any metrics data
        cursor = await db.execute(
            "SELECT COUNT(*) FROM research_runs WHERE metric_json IS NOT NULL AND metric_json != ''"
        )
        has_metrics = (await cursor.fetchone())[0] > 0

    return templates.TemplateResponse(
        request,
        "strategy.html",
        {
            "program_version": program[0] if program else 0,
            "program_content": program[1] if program else "",
            "versions": versions,
            "acquaintances": acquaintances,
            "runs": runs,
            "has_metrics": has_metrics,
            "auto_accept": settings.AUTO_ACCEPT_IMPROVEMENTS,
            "loop": _get_loop_state(request),
            "interval": settings.SCRAPE_INTERVAL_MINUTES,
            "active_nav": "strategy",
        },
    )


@router.get("/api/v1/strategy/loop-status-compact", response_class=HTMLResponse)
async def loop_status_compact(request: Request):
    """Compact one-line loop status for the Prospects page banner."""
    loop = _get_loop_state(request)
    if not loop.get("active"):
        dot = "background:var(--muted)"
        text = "Boucle inactive"
    elif loop.get("status") == "sleeping":
        dot = "background:var(--success)"
        text = f"Boucle active — {loop.get('cycles_completed', 0)} cycles, {loop.get('total_prospects_found', 0)} prospects trouves"
    elif loop.get("status") == "session_expired":
        dot = "background:var(--error)"
        text = "Session LinkedIn expiree"
    elif loop.get("last_error"):
        dot = "background:var(--error)"
        text = f"Erreur: {loop['last_error'][:60]}"
    else:
        dot = "background:var(--accent);animation:pulse 1s infinite"
        step = loop.get("current_step", "en cours...")
        text = step[:80]
    n8n_badge = '<span style="background:#6366f1;color:white;padding:0.05rem 0.35rem;border-radius:3px;font-size:0.7rem;font-weight:600;margin:0 0.3rem;">n8n</span>' if text.startswith('n8n:') else ''
    if text.startswith('n8n:'):
        text = text[4:].strip()
    return HTMLResponse(
        f'<span style="width:8px;height:8px;border-radius:50%;{dot};display:inline-block;flex-shrink:0;"></span> {n8n_badge}{text}'
    )


@router.get("/api/v1/strategy/rescore-status", response_class=HTMLResponse)
async def rescore_status():
    """Polled by UI to show re-scoring progress after program change."""
    from app.services.autoresearch_service import RESCORE_STATE
    s = RESCORE_STATE
    if not s["active"] and s["total"] == 0:
        return HTMLResponse("")

    pct = int((s["done"] / s["total"]) * 100) if s["total"] > 0 else 0
    active_class = "" if s["active"] else "display:none;"

    return HTMLResponse(f"""
    <div id="rescore-progress" style="margin-top:0.75rem;padding:0.75rem;border:1px solid var(--accent);
         border-radius:var(--radius-sm);background:var(--accent-dim);font-size:0.85rem;"
         hx-get="/api/v1/strategy/rescore-status" hx-trigger="every 3s" hx-swap="outerHTML">
      <div style="display:flex;justify-content:space-between;margin-bottom:0.4rem;">
        <strong>Re-scoring en cours ({s['triggered_by']})</strong>
        <span>{s['done']}/{s['total']} ({pct}%)</span>
      </div>
      <div style="height:6px;background:var(--surface-3);border-radius:3px;overflow:hidden;">
        <div style="height:100%;width:{pct}%;background:var(--accent);border-radius:3px;transition:width 0.3s;"></div>
      </div>
      <div style="margin-top:0.3rem;color:var(--muted);font-size:0.8rem;">{s['current']}</div>
    </div>
    """)


@router.get("/api/v1/strategy/loop-status", response_class=HTMLResponse)
async def loop_status(request: Request):
    """Polled every 5s by the UI to show live loop state."""
    loop = _get_loop_state(request)
    return HTMLResponse(
        templates.env.get_template("partials/loop_status.html").render({
            "request": request,
            "loop": loop,
            "interval": settings.SCRAPE_INTERVAL_MINUTES,
        })
    )


@router.post("/api/v1/strategy/loop/start", response_class=HTMLResponse)
async def start_loop(request: Request):
    """Activate the background research loop."""
    loop = _get_loop_state(request)
    loop["active"] = True
    loop["status"] = "sleeping"
    loop["current_step"] = "Boucle activee. Premier cycle demarre..."

    # Trigger immediate first run
    scheduler = request.app.state.scheduler
    job = scheduler.get_job("research_loop")
    if job:
        job.modify(next_run_time=datetime.now())

    return HTMLResponse(
        templates.env.get_template("partials/loop_status.html").render({
            "request": request,
            "loop": loop,
            "interval": settings.SCRAPE_INTERVAL_MINUTES,
        })
    )


@router.post("/api/v1/strategy/loop/stop", response_class=HTMLResponse)
async def stop_loop(request: Request):
    """Pause the background research loop."""
    loop = _get_loop_state(request)
    loop["active"] = False
    loop["status"] = "idle"
    loop["current_step"] = None

    return HTMLResponse(
        templates.env.get_template("partials/loop_status.html").render({
            "request": request,
            "loop": loop,
            "interval": settings.SCRAPE_INTERVAL_MINUTES,
        })
    )


@router.put("/api/v1/strategy/program")
async def save_program(content: str = Form(...)):
    async with get_db() as db:
        new_v = await research.apply_program(db, content, author="human")
        await db.execute(
            "UPDATE prospect_program SET trigger = 'manual', change_reason = 'Modification manuelle' WHERE version = ?",
            (new_v,),
        )
        await db.commit()
    return {"ok": True}


@router.post("/api/v1/strategy/propose", response_class=HTMLResponse)
async def propose_improvement():
    async with get_db() as db:
        result = await research.propose_program_improvement(db)

    import re
    analysis = result.get("analysis", "")
    analysis = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", analysis)
    analysis = re.sub(r"^### (.+)$", r"<h4 style='margin:0.5rem 0 0.25rem;font-size:0.9rem;'>\1</h4>", analysis, flags=re.MULTILINE)
    analysis = analysis.replace("\n", "<br>")

    proposed = result.get("proposed_program", "")
    version = result.get("current_version", 0)

    html = f"""<div style="border:1px solid var(--crm-blue);border-radius:0.5rem;padding:1rem;margin-top:1rem;">
        <h4 style="margin:0 0 0.5rem;">Proposition de Claude (v{version} → v{version + 1})</h4>
        <div style="font-size:0.85rem;max-height:300px;overflow-y:auto;">{analysis}</div>"""

    if proposed:
        # Escape for safe embedding in a form hidden field
        import html as html_mod
        safe_proposed = html_mod.escape(proposed)
        html += f"""<form hx-post="/api/v1/strategy/apply-proposal" hx-swap="none" style="margin-top:0.75rem;">
            <input type="hidden" name="content" value="{safe_proposed}">
            <button type="submit" class="outline" hx-confirm="Appliquer le programme propose par Claude ?">
                Appliquer v{version + 1}
            </button>
            <button type="button" class="outline secondary"
                    onclick="document.querySelector('textarea[name=content]').value = this.closest('form').querySelector('input[name=content]').value;">
                Copier dans l'editeur
            </button>
        </form>"""

    html += "</div>"
    return html


@router.post("/api/v1/strategy/apply-proposal")
async def apply_proposal(content: str = Form(...)):
    async with get_db() as db:
        version = await research.apply_program(db, content, author="claude")
    return {"ok": True, "version": version}


@router.post("/api/v1/strategy/restore/{version}")
async def restore_version(version: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT content FROM prospect_program WHERE version = ?", (version,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"error": "Version not found"}
        await research.apply_program(db, row[0], author="human-restore")
    return {"ok": True}


@router.post("/api/v1/strategy/acquaintances", response_class=HTMLResponse)
async def add_acquaintance(
    full_name: str = Form(...),
    headline: str = Form(""),
    company: str = Form(""),
    linkedin_url: str = Form(""),
    relationship: str = Form("prospect ideal"),
):
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO acquaintances (full_name, headline, company, linkedin_url, relationship, is_positive_example) VALUES (?, ?, ?, ?, ?, ?)",
            (full_name, headline, company, linkedin_url, relationship, 0 if relationship == "concurrent" else 1),
        )
        await db.commit()
        acq_id = cursor.lastrowid

    return f"""<div class="acq-card" style="display:flex;justify-content:space-between;align-items:center;padding:0.4rem 0;border-bottom:1px solid var(--pico-muted-border-color);font-size:0.85rem;">
        <div>
            <strong>{full_name}</strong>
            <span style="color:var(--pico-muted-color);"> — {headline}</span>
            <span style="color:var(--pico-muted-color);"> @ {company}</span>
            <span class="chip {'flag-negative' if relationship == 'concurrent' else 'trait'}">{relationship}</span>
        </div>
        <button class="outline secondary" style="font-size:0.7rem;padding:0.15rem 0.4rem;margin:0;"
                hx-delete="/api/v1/strategy/acquaintances/{acq_id}"
                hx-target="closest .acq-card"
                hx-swap="outerHTML">x</button>
    </div>"""


@router.delete("/api/v1/strategy/acquaintances/{acq_id}")
async def delete_acquaintance(acq_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM acquaintances WHERE id = ?", (acq_id,))
        await db.commit()
    return ""


@router.post("/api/v1/strategy/run", response_class=HTMLResponse)
async def trigger_research_run(request: Request):
    """Manual run: Claude generates queries → Patchright scrapes LinkedIn → score + store."""
    import json as _json
    from app.scraper.rate_limiter import DailyLimitReached
    from app.services.scoring_service import ScoringService
    from app.repositories.prospect_repo import ProspectRepository

    scraper = request.app.state.scraper

    async with get_db() as db:
        # Step 1: Claude generates queries from program
        queries = await research.generate_search_plan(db)
        if not queries:
            return _toast_html("Claude n'a genere aucune query.", "error")

        # Step 2: Start scraper browser if not running
        if not scraper._browser:
            try:
                await scraper.start()
            except Exception as e:
                return _toast_html(f"Impossible de demarrer le navigateur: {str(e)[:100]}", "error")

        # Step 3: Check LinkedIn session
        if not await scraper.is_session_valid():
            return _toast_html(
                "Session LinkedIn expiree. Connecte-toi sur le VPS : "
                "ssh -X openclaw@46.250.239.50 puis "
                "cd okoone-crm && .venv/bin/python -m app.scraper.session_manager --login",
                "error",
            )

        repo = ProspectRepository(db)
        scoring = ScoringService()
        total_found = 0
        total_new = 0
        errors: list[str] = []

        # Step 4: Scrape LinkedIn with Patchright (real scraping!)
        for q in queries[:10]:  # Max 10 per manual run
            kw = q["keywords"]
            loc = q.get("location")
            try:
                results = await scraper.search_people(kw, loc)
                total_found += len(results)

                cursor = await db.execute(
                    "INSERT INTO search_queries (keywords, location) VALUES (?, ?)",
                    (kw, loc),
                )
                await db.commit()

                for p in results:
                    username = (p.get("profile_username") or "").strip("/").split("/")[-1]
                    if not username or len(username) < 2:
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
                await db.commit()

            except DailyLimitReached as e:
                errors.append(f"Rate limit atteint: {e}")
                break
            except Exception as e:
                errors.append(f"'{kw[:25]}': {str(e)[:60]}")

        # Step 5: Score all unscored prospects
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
            weights = _json.loads(w_row[0])
            for pid in unscored:
                prospect = await repo.find_by_id(pid)
                if prospect:
                    score, breakdown = await scoring.score_prospect(prospect, weights)
                    summary = scoring.generate_score_summary(breakdown, weights, prospect)
                    await repo.update(pid, {
                        "relevance_score": score,
                        "score_breakdown": _json.dumps(breakdown),
                        "score_summary": summary,
                        "status": "screened",
                    })
                    scored += 1
            await db.commit()

        # Step 5.5: Claude deep analysis on newly scored prospects
        analyzed = 0
        try:
            from app.services.deep_analysis_service import DeepAnalysisService
            deep_svc = DeepAnalysisService()
            _prog_version, _prog_content = await research._load_program(db)
            _acqs = await research._load_acquaintances(db)

            # Prospects with experience but no Claude analysis (max 5 per run)
            da_cursor = await db.execute(
                "SELECT id FROM prospects "
                "WHERE experience_json IS NOT NULL AND experience_json != '' AND experience_json != '[]' "
                "AND (claude_analysis IS NULL OR claude_analysis = '') "
                "ORDER BY relevance_score DESC LIMIT 5"
            )
            da_ids = [row[0] for row in await da_cursor.fetchall()]

            for pid in da_ids:
                prospect = await repo.find_by_id(pid)
                if not prospect:
                    continue
                try:
                    analysis = await deep_svc.analyze_prospect(prospect, _prog_content, _acqs)
                    await repo.update(pid, {"claude_analysis": _json.dumps(analysis, ensure_ascii=False)})
                    analyzed += 1
                except Exception as e:
                    import logging as _log
                    _log.getLogger("okoone.deep_analysis").error(
                        "Manual run analysis error for %d: %s", pid, str(e)[:100]
                    )
            await db.commit()
        except Exception:
            pass  # non-blocking — scoring is more important

        # Step 6: Record run
        v_cursor = await db.execute(
            "SELECT version FROM prospect_program WHERE status = 'active' ORDER BY version DESC LIMIT 1"
        )
        v = await v_cursor.fetchone()
        await db.execute("""
            INSERT INTO research_runs
                (program_version, finished_at, status, prospects_found, prospects_qualified)
            VALUES (?, datetime('now'), ?, ?, ?)
        """, (v[0] if v else 0, "completed" if total_new > 0 else "no_results", total_new, scored))
        await db.commit()

        rate_stats = request.app.state.rate_limiter.get_stats()
        err_html = f"<br><span style='color:var(--error);'>{'; '.join(errors[:3])}</span>" if errors else ""

        if total_new == 0 and not errors:
            return _toast_html(
                f"{len(queries[:5])} queries executees mais 0 nouveaux prospects. "
                "Le scraper fonctionne mais les resultats sont vides ou deja connus. "
                "Essaie de modifier le Programme pour varier les recherches.",
                "warning",
            )

        analyzed_html = f" | {analyzed} analyses Claude" if analyzed > 0 else ""
        return HTMLResponse(f"""<div style="padding:1rem;border:1px solid var(--success);border-radius:var(--radius-sm);font-size:0.88rem;background:var(--success-dim);">
            <strong>Cycle termine</strong><br>
            {len(queries[:5])} queries | {total_found} resultats LinkedIn | {total_new} nouveaux prospects | {scored} scores{analyzed_html}<br>
            <span style="color:var(--muted);font-size:0.8rem;">
                Rate limits: {rate_stats['search']['used']}/{rate_stats['search']['limit']} recherches,
                {rate_stats['profile']['used']}/{rate_stats['profile']['limit']} profils
                (compte: {rate_stats['account_age_weeks']} sem.)
            </span>
            {err_html}
        </div>""")


def _toast_html(msg: str, kind: str = "info") -> HTMLResponse:
    color = {"error": "var(--error)", "success": "var(--success)"}.get(kind, "var(--info)")
    bg = {"error": "var(--error-dim)", "success": "var(--success-dim)"}.get(kind, "var(--info-dim)")
    return HTMLResponse(
        f'<div style="padding:0.75rem;border:1px solid {color};border-radius:var(--radius-sm);'
        f'font-size:0.88rem;background:{bg};">{msg}</div>'
    )


@router.post("/api/v1/strategy/accept-proposal/{run_id}")
async def accept_proposal(run_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT proposed_program FROM research_runs WHERE id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return {"error": "No proposal found"}

        await research.apply_program(db, row[0], author="claude-accepted")
        await db.execute(
            "UPDATE research_runs SET proposal_status = 'accepted' WHERE id = ?", (run_id,)
        )
        await db.commit()
    return {"ok": True}


@router.get("/api/v1/strategy/metrics", response_class=JSONResponse)
async def get_metrics_trend():
    """Return metrics history for the last 20 cycles + top queries."""
    async with get_db() as db:
        # Metrics per cycle
        cursor = await db.execute("""
            SELECT id, program_version, started_at, finished_at,
                   prospects_found, prospects_qualified, metric_json,
                   status, proposal_status
            FROM research_runs
            WHERE metric_json IS NOT NULL AND metric_json != ''
            ORDER BY started_at DESC
            LIMIT 20
        """)
        runs_raw = await cursor.fetchall()

        cycles = []
        for r in runs_raw:
            metrics = {}
            try:
                metrics = json.loads(r[6]) if r[6] else {}
            except (json.JSONDecodeError, TypeError):
                pass
            cycles.append({
                "run_id": r[0],
                "program_version": r[1],
                "started_at": r[2],
                "finished_at": r[3],
                "prospects_found": r[4],
                "prospects_qualified": r[5],
                "status": r[7],
                "proposal_status": r[8],
                "qualification_rate": metrics.get("qualification_rate"),
                "novelty_rate": metrics.get("novelty_rate"),
                "diversity_score": metrics.get("diversity_score"),
                "avg_score": metrics.get("avg_score"),
            })

        # Top performing queries
        cursor = await db.execute("""
            SELECT search_keywords, search_location,
                   SUM(prospects_found) as total_found,
                   SUM(prospects_new) as total_new,
                   AVG(avg_score) as mean_score,
                   MAX(best_score) as top_score,
                   SUM(qualified_count) as total_qualified,
                   COUNT(*) as times_used
            FROM query_performance
            GROUP BY search_keywords, search_location
            ORDER BY mean_score DESC, total_qualified DESC
            LIMIT 15
        """)
        queries_raw = await cursor.fetchall()

        top_queries = []
        for q in queries_raw:
            top_queries.append({
                "keywords": q[0],
                "location": q[1],
                "total_found": q[2],
                "total_new": q[3],
                "mean_score": round(q[4], 1) if q[4] else 0,
                "top_score": round(q[5], 1) if q[5] else 0,
                "total_qualified": q[6],
                "times_used": q[7],
            })

    return JSONResponse({
        "cycles": list(reversed(cycles)),  # chronological order
        "top_queries": top_queries,
        "auto_accept": settings.AUTO_ACCEPT_IMPROVEMENTS,
    })


@router.get("/api/v1/strategy/metrics-panel", response_class=HTMLResponse)
async def metrics_panel():
    """Render the metrics trend panel as HTML (htmx partial)."""
    async with get_db() as db:
        cursor = await db.execute("""
            SELECT id, program_version, started_at, prospects_found,
                   prospects_qualified, metric_json, status
            FROM research_runs
            WHERE metric_json IS NOT NULL AND metric_json != ''
            ORDER BY started_at DESC LIMIT 10
        """)
        runs_raw = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT search_keywords, search_location,
                   AVG(avg_score) as mean_score,
                   SUM(qualified_count) as total_qualified,
                   SUM(prospects_found) as total_found,
                   COUNT(*) as times_used
            FROM query_performance
            GROUP BY search_keywords, search_location
            HAVING total_found > 0
            ORDER BY mean_score DESC
            LIMIT 10
        """)
        queries_raw = await cursor.fetchall()

    if not runs_raw:
        return HTMLResponse(
            '<p style="color:var(--muted);font-size:0.88rem;">Pas encore de metriques. '
            'Les metriques apparaitront apres le premier cycle complet.</p>'
        )

    # Build metrics table rows
    rows_html = ""
    for r in reversed(list(runs_raw)):
        metrics = {}
        try:
            metrics = json.loads(r[5]) if r[5] else {}
        except (json.JSONDecodeError, TypeError):
            pass

        qual_rate = metrics.get("qualification_rate", "—")
        novelty = metrics.get("novelty_rate", "—")
        diversity = metrics.get("diversity_score", "—")
        avg_s = metrics.get("avg_score", "—")

        qual_color = "var(--success)" if isinstance(qual_rate, (int, float)) and qual_rate > 30 else "var(--text)"
        novelty_color = "var(--warning)" if isinstance(novelty, (int, float)) and novelty < 20 else "var(--text)"

        rows_html += f"""<tr>
            <td style="font-size:0.8rem;color:var(--muted);">{r[2][:16] if r[2] else '—'}</td>
            <td>v{r[1]}</td>
            <td>{r[3]}</td>
            <td style="color:{qual_color};font-weight:600;">{qual_rate}{'%' if isinstance(qual_rate, (int, float)) else ''}</td>
            <td style="color:{novelty_color};">{novelty}{'%' if isinstance(novelty, (int, float)) else ''}</td>
            <td>{diversity}</td>
            <td>{avg_s}</td>
        </tr>"""

    # Build query performance rows
    queries_html = ""
    for q in queries_raw:
        mean_s = round(q[2], 1) if q[2] else 0
        score_color = "var(--success)" if mean_s > 40 else "var(--warning)" if mean_s > 20 else "var(--error)"
        queries_html += f"""<tr>
            <td style="font-size:0.85rem;max-width:200px;overflow:hidden;text-overflow:ellipsis;">{q[0]}</td>
            <td style="color:var(--muted);">{q[1] or 'any'}</td>
            <td style="color:{score_color};font-weight:600;">{mean_s}</td>
            <td>{q[3]}/{q[4]}</td>
            <td style="color:var(--muted);">{q[5]}x</td>
        </tr>"""

    html = f"""
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;">
      <div>
        <h4 style="margin:0 0 0.5rem;font-size:0.9rem;">Tendance des metriques</h4>
        <div style="overflow-x:auto;">
          <table style="font-size:0.82rem;">
            <thead><tr>
              <th>Date</th><th>Prog</th><th>Trouves</th>
              <th>Qual%</th><th>Nouv%</th><th>Div</th><th>Avg</th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
      </div>
      <div>
        <h4 style="margin:0 0 0.5rem;font-size:0.9rem;">Queries les plus performantes</h4>
        <div style="overflow-x:auto;">
          <table style="font-size:0.82rem;">
            <thead><tr>
              <th>Keywords</th><th>Loc</th><th>Score moy</th><th>Qual/Total</th><th>Utilise</th>
            </tr></thead>
            <tbody>{queries_html if queries_html else '<tr><td colspan="5" style="color:var(--muted);">Pas encore de donnees query</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    </div>"""

    return HTMLResponse(html)
