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
async def trigger_research_run():
    """Trigger a full autoresearch cycle. This is the main loop entry point."""
    # Import here to avoid circular deps
    from app.services.scraper_service import ScraperService
    from app.scraper.linkedin import LinkedInScraper
    from app.scraper.rate_limiter import RateLimiter
    from app.services.prospect_service import ProspectService
    from app.services.scoring_service import ScoringService
    from app.repositories.prospect_repo import ProspectRepository
    from app.repositories.search_repo import SearchRepository

    async with get_db() as db:
        rate_limiter = RateLimiter()
        scraper = LinkedInScraper(
            profile_dir="/home/openclaw/.linkedin-mcp/profile",
            rate_limiter=rate_limiter,
        )
        prospect_repo = ProspectRepository(db)
        search_repo = SearchRepository(db)
        scoring = ScoringService()
        prospect_service = ProspectService(prospect_repo, scoring)
        scraper_service = ScraperService(scraper, prospect_service, search_repo, rate_limiter)

        try:
            result = await research.run_full_cycle(db, scraper_service)
            return f"""<div style="padding:0.75rem;border:1px solid var(--crm-green);border-radius:0.5rem;font-size:0.85rem;">
                Cycle termine : {result.get('queries_generated', 0)} queries, {result.get('prospects_found', 0)} prospects trouves.
                <br>Rechargez la page pour voir les resultats.
            </div>"""
        except Exception as e:
            return f"""<div style="padding:0.75rem;border:1px solid var(--crm-red);border-radius:0.5rem;font-size:0.85rem;">
                Erreur: {str(e)[:200]}
            </div>"""


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
