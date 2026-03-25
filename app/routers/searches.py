from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db
from app.models import SearchQueryCreate
from app.repositories.search_repo import SearchRepository

router = APIRouter(prefix="/api/v1/searches", tags=["searches"])
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def list_searches():
    """List all search queries."""
    async with get_db() as db:
        repo = SearchRepository(db)
        searches = await repo.list_all()
    return {"searches": searches}


@router.post("/")
async def create_search(request: Request):
    """Create a new search query. Returns HTML row for htmx or JSON."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    keywords = body.get("keywords", "").strip()
    if not keywords:
        raise HTTPException(status_code=400, detail="Mots-cles requis")

    is_recurring = body.get("is_recurring")
    if isinstance(is_recurring, str):
        is_recurring = is_recurring.lower() in ("true", "1", "on")

    data = {
        "keywords": keywords,
        "location": body.get("location") or None,
        "filters_json": body.get("filters_json") or None,
        "is_recurring": 1 if is_recurring else 0,
        "recurrence_cron": body.get("recurrence_cron") or None,
    }

    async with get_db() as db:
        repo = SearchRepository(db)
        new_id = await repo.create(data)
        search = await repo.get_by_id(new_id)

    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx and search:
        s = search
        return HTMLResponse(f"""
        <tr id="search-row-{s['id']}">
          <td><strong>{s['keywords']}</strong></td>
          <td>{s.get('location') or '—'}</td>
          <td>Jamais</td>
          <td>0</td>
          <td>—</td>
          <td><span class="pill pill-qualified">Active</span></td>
          <td>{s.get('recurrence_cron') or 'Non'}</td>
          <td>
            <button class="outline" style="font-size:0.75rem; padding:0.2rem 0.5rem; margin:0;"
                    hx-post="/api/v1/searches/{s['id']}/run"
                    hx-target="#search-row-{s['id']}"
                    hx-swap="outerHTML">Lancer</button>
          </td>
        </tr>
        """)

    return {"id": new_id, "search": search}


@router.post("/{search_id}/run")
async def trigger_run(search_id: int):
    """Trigger a manual scrape run for a search query."""
    async with get_db() as db:
        repo = SearchRepository(db)
        search = await repo.get_by_id(search_id)
        if not search:
            raise HTTPException(status_code=404, detail="Recherche introuvable")

        run_id = await repo.create_scrape_run(search_id)

    # In production, this would dispatch to the scraper service async.
    # For now, return confirmation.
    return {
        "message": f"Scrape lance pour la recherche #{search_id}",
        "run_id": run_id,
    }


@router.patch("/{search_id}")
async def update_search(search_id: int, request: Request):
    """Toggle active/paused status of a search query."""
    body = await request.json()

    async with get_db() as db:
        repo = SearchRepository(db)
        search = await repo.get_by_id(search_id)
        if not search:
            raise HTTPException(status_code=404, detail="Recherche introuvable")

        is_active = body.get("is_active")
        if isinstance(is_active, str):
            is_active = is_active.lower() in ("true", "1")
        if isinstance(is_active, bool):
            is_active = 1 if is_active else 0

        await db.execute(
            "UPDATE search_queries SET is_active = ? WHERE id = ?",
            (is_active, search_id),
        )
        await db.commit()

        updated = await repo.get_by_id(search_id)

    return updated
