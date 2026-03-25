from __future__ import annotations

import json
import math
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db
from app.repositories.prospect_repo import ProspectRepository

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="templates")

PAGE_SIZE = 50


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect to /prospects."""
    return RedirectResponse(url="/prospects", status_code=302)


@router.get("/prospects", response_class=HTMLResponse)
async def prospect_list(
    request: Request,
    status: Optional[str] = None,
    min_score: Optional[float] = None,
    sort: str = "relevance_score",
    order: str = "desc",
    q: Optional[str] = None,
    page: int = 1,
):
    """Main dashboard with prospect table. Supports htmx partial (HX-Request header)."""
    async with get_db() as db:
        repo = ProspectRepository(db)

        if q and q.strip():
            prospects = await repo.search_fulltext(q.strip(), limit=PAGE_SIZE)
            total = len(prospects)
        else:
            offset = (page - 1) * PAGE_SIZE
            prospects, total = await repo.list_all(
                status=status if status else None,
                min_score=min_score,
                sort_by=sort,
                order=order,
                limit=PAGE_SIZE,
                offset=offset,
            )

        total_pages = max(1, math.ceil(total / PAGE_SIZE))

        # Fetch campaign list for bulk enrollment dropdown
        cursor = await db.execute(
            "SELECT id, name FROM email_campaigns WHERE status != 'completed' ORDER BY name"
        )
        campaigns = [dict(r) for r in await cursor.fetchall()]

    filters = {
        "status": status or "",
        "min_score": min_score or 0,
        "q": q or "",
        "sort": sort,
        "order": order,
    }

    # If htmx partial request, return only table rows
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        html_parts = []
        for p in prospects:
            html_parts.append(
                templates.get_template("partials/prospect_row.html").render(
                    {"request": request, "p": p}
                )
            )
        if not html_parts:
            html_parts.append(
                '<tr><td colspan="8" class="empty-state"><p>Aucun prospect trouve.</p></td></tr>'
            )
        return HTMLResponse("".join(html_parts))

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "prospects": prospects,
            "page": page,
            "total_pages": total_pages,
            "filters": filters,
            "campaigns": campaigns,
            "active_nav": "prospects",
        },
    )


@router.get("/prospects/{prospect_id}", response_class=HTMLResponse)
async def prospect_detail(request: Request, prospect_id: int):
    """Full prospect profile view."""
    async with get_db() as db:
        repo = ProspectRepository(db)
        prospect = await repo.find_by_id(prospect_id)
        if not prospect:
            return HTMLResponse("<h2>Prospect introuvable</h2>", status_code=404)

        # Parse JSON fields safely
        score_breakdown = {}
        if prospect.get("score_breakdown"):
            try:
                score_breakdown = json.loads(prospect["score_breakdown"])
            except (json.JSONDecodeError, TypeError):
                pass

        experiences = []
        if prospect.get("experience_json"):
            try:
                experiences = json.loads(prospect["experience_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        educations = []
        if prospect.get("education_json"):
            try:
                educations = json.loads(prospect["education_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        traits = []
        if prospect.get("traits_json"):
            try:
                traits = json.loads(prospect["traits_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        flags = []
        if prospect.get("flags_json"):
            try:
                flags = json.loads(prospect["flags_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Fetch last human review
        review_cursor = await db.execute(
            "SELECT * FROM human_reviews WHERE prospect_id = ? ORDER BY reviewed_at DESC LIMIT 1",
            (prospect_id,),
        )
        last_review_row = await review_cursor.fetchone()
        last_review = dict(last_review_row) if last_review_row else None

        # Fetch campaign enrollments
        enroll_cursor = await db.execute(
            """
            SELECT ee.*, ec.name as campaign_name
            FROM email_enrollments ee
            JOIN email_campaigns ec ON ee.campaign_id = ec.id
            WHERE ee.prospect_id = ?
            """,
            (prospect_id,),
        )
        enrollments = [dict(r) for r in await enroll_cursor.fetchall()]

    return templates.TemplateResponse(
        "prospect_detail.html",
        {
            "request": request,
            "prospect": prospect,
            "score_breakdown": score_breakdown,
            "experiences": experiences,
            "educations": educations,
            "traits": traits,
            "flags": flags,
            "last_review": last_review,
            "enrollments": enrollments,
            "contact_email": prospect.get("contact_email"),
            "active_nav": "prospects",
        },
    )


@router.get("/searches", response_class=HTMLResponse)
async def searches_page(request: Request):
    """Searches management page."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM search_queries ORDER BY created_at DESC"
        )
        searches = [dict(r) for r in await cursor.fetchall()]

    return templates.TemplateResponse(
        "searches.html",
        {
            "request": request,
            "searches": searches,
            "active_nav": "searches",
        },
    )


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    """Campaigns management page."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT ec.*,
                   (SELECT COUNT(*) FROM email_enrollments WHERE campaign_id = ec.id) as enrolled_count,
                   (SELECT COUNT(*) FROM email_sends WHERE enrollment_id IN
                        (SELECT id FROM email_enrollments WHERE campaign_id = ec.id)
                        AND status = 'sent') as sent_count,
                   (SELECT COUNT(*) FROM email_sends WHERE enrollment_id IN
                        (SELECT id FROM email_enrollments WHERE campaign_id = ec.id)
                        AND status = 'replied') as reply_count
            FROM email_campaigns ec
            ORDER BY ec.created_at DESC
            """
        )
        campaigns = [dict(r) for r in await cursor.fetchall()]

    return templates.TemplateResponse(
        "campaigns.html",
        {
            "request": request,
            "campaigns": campaigns,
            "active_nav": "campaigns",
        },
    )


@router.get("/eval", response_class=HTMLResponse)
async def eval_dashboard(request: Request):
    """Evaluation dashboard with weights, metrics, and signals."""
    async with get_db() as db:
        # Active scoring weights
        weights_cursor = await db.execute(
            "SELECT * FROM scoring_weights WHERE is_active = 1 LIMIT 1"
        )
        weights_row = await weights_cursor.fetchone()
        current_weights = {}
        weights_json = "[]"
        if weights_row:
            try:
                current_weights = json.loads(dict(weights_row)["criteria_json"])
                weights_json = json.dumps(
                    [{"name": k, "value": v} for k, v in current_weights.items()]
                )
            except (json.JSONDecodeError, TypeError):
                pass

        # Latest eval snapshots
        snap_cursor = await db.execute(
            "SELECT * FROM eval_snapshots ORDER BY created_at DESC LIMIT 20"
        )
        snapshots = [dict(r) for r in await snap_cursor.fetchall()]

        # Latest metrics from most recent snapshot
        metrics = {}
        if snapshots:
            latest = snapshots[0]
            metrics = {
                "precision": latest.get("precision_score", 0) or 0,
                "recall": latest.get("recall_score", 0) or 0,
                "f1": latest.get("f1_score", 0) or 0,
                "agreement_rate": latest.get("human_agreement_rate", 0) or 0,
            }

        # Learning signals
        sig_cursor = await db.execute(
            "SELECT * FROM learning_signals ORDER BY created_at DESC LIMIT 20"
        )
        signals = [dict(r) for r in await sig_cursor.fetchall()]

    return templates.TemplateResponse(
        "eval.html",
        {
            "request": request,
            "current_weights": current_weights,
            "weights_json": weights_json,
            "metrics": metrics,
            "snapshots": snapshots,
            "signals": signals,
            "proposed_weights": None,
            "active_nav": "eval",
        },
    )
