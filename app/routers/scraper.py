from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.database import get_db
from app.repositories.search_repo import SearchRepository

router = APIRouter(prefix="/api/v1/scraper", tags=["scraper"])


@router.get("/status")
async def scraper_status(request: Request):
    """Session health, rate limits remaining, last run. Returns HTML partial for footer indicator."""
    async with get_db() as db:
        # Check active sessions
        session_cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM linkedin_sessions WHERE is_active = 1"
        )
        session_row = await session_cursor.fetchone()
        active_sessions = session_row[0] if session_row else 0

        # Check latest scrape run
        run_cursor = await db.execute(
            "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT 1"
        )
        last_run_row = await run_cursor.fetchone()
        last_run = dict(last_run_row) if last_run_row else None

        # Count today's runs for rate limit estimate
        today_cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM scrape_runs WHERE date(started_at) = date('now')"
        )
        today_row = await today_cursor.fetchone()
        runs_today = today_row[0] if today_row else 0

    daily_search_limit = settings.LINKEDIN_DAILY_SEARCH_LIMIT
    remaining = max(0, daily_search_limit - runs_today)

    status_class = "ok"
    status_text = f"Scraper OK — {remaining} recherches restantes"
    if active_sessions == 0:
        status_class = "offline"
        status_text = "Scraper hors ligne — aucune session"
    elif remaining <= 5:
        status_class = "warning"
        status_text = f"Attention — {remaining} recherches restantes"
    elif last_run and last_run.get("status") == "error":
        status_class = "error"
        status_text = "Erreur derniere execution"

    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        return HTMLResponse(f"""
        <span class="scraper-status">
          <span class="scraper-dot {status_class}"></span> {status_text}
        </span>
        """)

    return {
        "active_sessions": active_sessions,
        "runs_today": runs_today,
        "remaining_searches": remaining,
        "last_run": last_run,
        "status": status_class,
        "message": status_text,
    }


@router.post("/run-all")
async def run_all_recurring():
    """Trigger scrape for all active recurring search queries."""
    async with get_db() as db:
        repo = SearchRepository(db)
        recurring = await repo.get_active_recurring()

        run_ids = []
        for search in recurring:
            run_id = await repo.create_scrape_run(search["id"])
            run_ids.append({"search_id": search["id"], "run_id": run_id})

    # In production, each run_id would be dispatched to the scraper service.
    return {
        "message": f"{len(run_ids)} recherches lancees",
        "runs": run_ids,
    }
