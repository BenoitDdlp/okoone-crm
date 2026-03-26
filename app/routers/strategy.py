from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
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

    return templates.TemplateResponse(
        request,
        "strategy.html",
        {
            "program_version": program[0] if program else 0,
            "program_content": program[1] if program else "",
            "versions": versions,
            "acquaintances": acquaintances,
            "runs": runs,
            "loop": _get_loop_state(request),
            "interval": settings.SCRAPE_INTERVAL_MINUTES,
            "active_nav": "strategy",
        },
    )


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
        await research.apply_program(db, content, author="human")
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
        for q in queries[:5]:  # Max 5 per manual run (new account safety)
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
                    await repo.update(pid, {
                        "relevance_score": score,
                        "score_breakdown": _json.dumps(breakdown),
                        "status": "screened",
                    })
                    scored += 1
            await db.commit()

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

        return HTMLResponse(f"""<div style="padding:1rem;border:1px solid var(--success);border-radius:var(--radius-sm);font-size:0.88rem;background:var(--success-dim);">
            <strong>Cycle termine</strong><br>
            {len(queries[:5])} queries | {total_found} resultats LinkedIn | {total_new} nouveaux prospects | {scored} scores<br>
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
