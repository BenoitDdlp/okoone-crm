from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return RedirectResponse(url="/prospects", status_code=302)


@router.get("/prospects", response_class=HTMLResponse)
async def pipeline(request: Request, page: int = 1):
    """Main pipeline view — card-based prospect qualification."""
    PAGE_SIZE = 20

    async with get_db() as db:
        # Stats
        cursor = await db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status IN ('discovered', 'screened') THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'qualified' THEN 1 ELSE 0 END) as qualified,
                SUM(CASE WHEN status = 'contacted' THEN 1 ELSE 0 END) as contacted
            FROM prospects
        """)
        stats = dict(await cursor.fetchone())

        # Next prospect to review (unreviewed, ordered by score desc)
        cursor = await db.execute("""
            SELECT p.* FROM prospects p
            LEFT JOIN human_reviews hr ON hr.prospect_id = p.id
            WHERE hr.id IS NULL AND p.status NOT IN ('rejected', 'converted')
            ORDER BY p.relevance_score DESC
            LIMIT 1
        """)
        row = await cursor.fetchone()
        prospect = dict(row) if row else None

        experiences = []
        education = []
        skills = []
        traits = []
        score_breakdown: Optional[dict] = None
        claude_analysis: Optional[dict] = None
        if prospect:
            if prospect.get("experience_json"):
                try:
                    experiences = json.loads(prospect["experience_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if prospect.get("education_json"):
                try:
                    education = json.loads(prospect["education_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if prospect.get("skills_json"):
                try:
                    skills = json.loads(prospect["skills_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if prospect.get("traits_json"):
                try:
                    traits = json.loads(prospect["traits_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if prospect.get("score_breakdown"):
                try:
                    parsed = json.loads(prospect["score_breakdown"])
                    if isinstance(parsed, dict):
                        score_breakdown = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            if prospect.get("claude_analysis"):
                try:
                    parsed_ca = json.loads(prospect["claude_analysis"])
                    if isinstance(parsed_ca, dict):
                        claude_analysis = parsed_ca
                except (json.JSONDecodeError, TypeError):
                    pass

        # Recent decisions
        cursor = await db.execute("""
            SELECT p.full_name, p.headline, hr.reviewer_verdict as verdict
            FROM human_reviews hr
            JOIN prospects p ON p.id = hr.prospect_id
            ORDER BY hr.reviewed_at DESC LIMIT 8
        """)
        recent_decisions = [dict(r) for r in await cursor.fetchall()]

        # All prospects (paginated)
        offset = (page - 1) * PAGE_SIZE
        all_cursor = await db.execute(
            "SELECT * FROM prospects ORDER BY relevance_score DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset),
        )
        all_prospects = [dict(r) for r in await all_cursor.fetchall()]
        total_cursor = await db.execute("SELECT COUNT(*) FROM prospects")
        total_count = (await total_cursor.fetchone())[0]
        total_pages = max(1, -(-total_count // PAGE_SIZE))

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "prospect": prospect,
            "experiences": experiences,
            "education": education,
            "skills": skills,
            "traits": traits,
            "score_breakdown": score_breakdown,
            "claude_analysis": claude_analysis,
            "recent_decisions": recent_decisions,
            "all_prospects": all_prospects,
            "page": page,
            "total_pages": total_pages,
            "active_nav": "pipeline",
        },
    )


@router.get("/searches", response_class=HTMLResponse)
async def searches_page(request: Request):
    return RedirectResponse(url="/strategy", status_code=302)


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    """Outreach page."""
    async with get_db() as db:
        cursor = await db.execute("""
            SELECT ec.*,
                   (SELECT COUNT(*) FROM email_enrollments WHERE campaign_id = ec.id) as enrolled_count,
                   (SELECT COUNT(*) FROM email_sends es
                    JOIN email_enrollments ee ON es.enrollment_id = ee.id
                    WHERE ee.campaign_id = ec.id AND es.status = 'sent') as sent_count
            FROM email_campaigns ec ORDER BY ec.created_at DESC
        """)
        campaigns = [dict(r) for r in await cursor.fetchall()]

    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {"campaigns": campaigns, "active_nav": "outreach"},
    )


@router.get("/eval", response_class=HTMLResponse)
async def eval_redirect(request: Request):
    return RedirectResponse(url="/strategy", status_code=302)
