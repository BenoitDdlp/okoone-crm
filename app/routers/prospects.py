from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import logging
import re

from app.database import get_db
from app.models import HumanReviewCreate, ProspectUpdate
from app.repositories.prospect_repo import ProspectRepository

logger = logging.getLogger("okoone.prospects")


async def _feedback_program_revision(feedback: str, prospect_name: str, verdict: str) -> None:
    """Revise the program based on human feedback (runs in background).

    When the user gives feedback like 'pas de cambodge' or 'plus de startups SaaS',
    Claude reads the current program + the feedback and produces a revised version.
    This is the human-in-the-loop part of the Karpathy pattern.
    """
    try:
        from app.services.claude_advisor import _call_claude
        from app.services.autoresearch_service import AutoresearchService

        svc = AutoresearchService()
        async with get_db() as db:
            version, program = await svc._load_program(db)

            prompt = f"""## Programme actuel (v{version})
{program}

## Feedback humain (sur le prospect "{prospect_name}", verdict: {verdict})
{feedback}

---

Revise le programme de recherche en tenant compte de ce feedback.
Le feedback vient d'un humain qui qualifie les prospects — c'est TA METRIQUE PRINCIPALE.
Integre le feedback dans le programme de maniere permanente.

IMPORTANT:
- Retourne le programme COMPLET revise (pas juste les diffs)
- Le programme DOIT commencer par "# Prospect Research Program"
- Garde toutes les sections existantes (Objectif, Profil cible, etc.)
- Integre le feedback comme nouvelle regle ou ajustement

```
# Prospect Research Program v{version + 1}

[... programme complet revise ...]
```"""

            text = await _call_claude(prompt, system="")

            # Extract program from code block
            proposed = ""
            for pattern in [r"```(?:markdown)?\n(.*?)\n```", r"```\n(.*?)\n```", r"```(.*?)```"]:
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    proposed = match.group(1).strip()
                    break

            if proposed and svc._is_valid_program(proposed):
                new_version = await svc.apply_program(db, proposed, author="feedback")
                # Set trigger and reason on the new version
                await db.execute(
                    "UPDATE prospect_program SET trigger = 'feedback', change_reason = ? WHERE version = ?",
                    (f"Feedback on {prospect_name} ({verdict}): {feedback[:200]}", new_version),
                )
                await db.commit()
                logger.info("FEEDBACK REVISION: program updated to v%d based on: %s", new_version, feedback[:80])
            else:
                logger.warning("FEEDBACK REVISION: Claude's response was not a valid program")

    except Exception:
        logger.error("FEEDBACK REVISION failed:", exc_info=True)

router = APIRouter(prefix="/api/v1/prospects", tags=["prospects"])
templates = Jinja2Templates(directory="templates")

PAGE_SIZE = 50


@router.get("/")
async def list_prospects(
    status: Optional[str] = None,
    min_score: Optional[float] = None,
    sort: str = "relevance_score",
    order: str = "desc",
    q: Optional[str] = None,
    page: int = 1,
    limit: int = PAGE_SIZE,
):
    """List prospects with filters, sort, search, and pagination."""
    async with get_db() as db:
        repo = ProspectRepository(db)

        if q and q.strip():
            prospects = await repo.search_fulltext(q.strip(), limit=limit)
            total = len(prospects)
        else:
            offset = (page - 1) * limit
            prospects, total = await repo.list_all(
                status=status,
                min_score=min_score,
                sort_by=sort,
                order=order,
                limit=limit,
                offset=offset,
            )

    return {
        "prospects": prospects,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / limit)),
    }


@router.get("/stats")
async def get_stats(request: Request, partial: Optional[str] = None):
    """Prospect counts by status. If partial=1, return HTML partial."""
    async with get_db() as db:
        repo = ProspectRepository(db)
        by_status = await repo.count_by_status()
        total = sum(by_status.values())

    if partial == "1":
        return HTMLResponse(
            templates.get_template("partials/stats_bar.html").render(
                {"request": request, "total": total, "by_status": by_status}
            )
        )

    return {"total": total, "by_status": by_status}


@router.get("/{prospect_id}")
async def get_prospect(prospect_id: int):
    """Get a single prospect by ID."""
    async with get_db() as db:
        repo = ProspectRepository(db)
        prospect = await repo.find_by_id(prospect_id)
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect introuvable")
    return prospect


@router.get("/{prospect_id}/analysis")
async def get_prospect_analysis(prospect_id: int):
    """Return the Claude deep analysis for a prospect.

    If no analysis exists yet, triggers one on-the-fly (takes ~30s).
    """
    async with get_db() as db:
        repo = ProspectRepository(db)
        prospect = await repo.find_by_id(prospect_id)
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect introuvable")

        # Return cached analysis if available
        if prospect.get("claude_analysis"):
            try:
                return json.loads(prospect["claude_analysis"])
            except (json.JSONDecodeError, TypeError):
                return {"raw": prospect["claude_analysis"]}

        # No cached analysis — generate one on-the-fly
        has_experience = (
            prospect.get("experience_json")
            and prospect["experience_json"] not in ("", "[]")
        )
        if not has_experience:
            raise HTTPException(
                status_code=422,
                detail="Prospect sans donnees d'experience. Lance un deep screen d'abord.",
            )

        from app.services.deep_analysis_service import DeepAnalysisService
        from app.services.autoresearch_service import AutoresearchService

        deep_svc = DeepAnalysisService()
        ar_svc = AutoresearchService()
        _version, program_content = await ar_svc._load_program(db)
        acquaintances = await ar_svc._load_acquaintances(db)

        try:
            analysis = await deep_svc.analyze_prospect(prospect, program_content, acquaintances)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Erreur Claude CLI: {str(exc)[:200]}",
            )

        # Persist for future calls
        await repo.update(prospect_id, {
            "claude_analysis": json.dumps(analysis, ensure_ascii=False),
        })

        return analysis


@router.post("/{prospect_id}/analysis/refresh")
async def refresh_prospect_analysis(prospect_id: int):
    """Force a fresh Claude analysis for a prospect, overwriting any cached one."""
    async with get_db() as db:
        repo = ProspectRepository(db)
        prospect = await repo.find_by_id(prospect_id)
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect introuvable")

        has_experience = (
            prospect.get("experience_json")
            and prospect["experience_json"] not in ("", "[]")
        )
        if not has_experience:
            raise HTTPException(
                status_code=422,
                detail="Prospect sans donnees d'experience. Lance un deep screen d'abord.",
            )

        from app.services.deep_analysis_service import DeepAnalysisService
        from app.services.autoresearch_service import AutoresearchService

        deep_svc = DeepAnalysisService()
        ar_svc = AutoresearchService()
        _version, program_content = await ar_svc._load_program(db)
        acquaintances = await ar_svc._load_acquaintances(db)

        try:
            analysis = await deep_svc.analyze_prospect(prospect, program_content, acquaintances)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Erreur Claude CLI: {str(exc)[:200]}",
            )

        await repo.update(prospect_id, {
            "claude_analysis": json.dumps(analysis, ensure_ascii=False),
        })

        return analysis


@router.patch("/{prospect_id}")
async def update_prospect(prospect_id: int, data: ProspectUpdate):
    """Update prospect fields."""
    async with get_db() as db:
        repo = ProspectRepository(db)
        existing = await repo.find_by_id(prospect_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Prospect introuvable")

        update_data = data.model_dump(exclude_unset=True)
        if not update_data:
            return existing

        await repo.update(prospect_id, update_data)
        updated = await repo.find_by_id(prospect_id)
    return updated


@router.post("/{prospect_id}/review")
async def review_prospect(
    request: Request,
    prospect_id: int,
):
    """Submit a human review (approve/reject/flag). Returns updated row partial for htmx."""
    # Accept both JSON and form data
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    verdict = body.get("verdict", "flag")
    feedback_text = body.get("feedback_text")
    relevance_override = body.get("relevance_override")

    if verdict not in ("approve", "reject", "flag"):
        raise HTTPException(status_code=400, detail="Verdict invalide")

    async with get_db() as db:
        repo = ProspectRepository(db)
        prospect = await repo.find_by_id(prospect_id)
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect introuvable")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            """
            INSERT INTO human_reviews (prospect_id, reviewer_verdict, relevance_override, feedback_text, reviewed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (prospect_id, verdict, relevance_override, feedback_text, now),
        )

        # Update prospect status based on verdict
        status_map = {"approve": "qualified", "reject": "rejected", "flag": prospect["status"]}
        new_status = status_map[verdict]
        update_fields: dict = {"status": new_status}
        if relevance_override is not None:
            update_fields["relevance_score"] = float(relevance_override)

        await repo.update(prospect_id, update_fields)

        # If feedback is substantial, trigger an immediate program revision
        if feedback_text and len(feedback_text.strip()) > 10:
            import asyncio
            asyncio.create_task(_feedback_program_revision(
                feedback_text, prospect.get("full_name", ""), verdict
            ))
        await db.commit()

        updated = await repo.find_by_id(prospect_id)

        # For htmx: return the next review card (pipeline flow)
        is_htmx = request.headers.get("HX-Request") == "true"
        if is_htmx:
            # Fetch next unreviewed prospect
            cursor = await db.execute("""
                SELECT p.* FROM prospects p
                LEFT JOIN human_reviews hr ON hr.prospect_id = p.id
                WHERE hr.id IS NULL AND p.status NOT IN ('rejected', 'converted')
                ORDER BY p.relevance_score DESC LIMIT 1
            """)
            next_row = await cursor.fetchone()

            if next_row:
                next_p = dict(next_row)
                experiences = []
                education = []
                skills = []
                traits = []
                score_breakdown = {}
                if next_p.get("experience_json"):
                    try:
                        experiences = json.loads(next_p["experience_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if next_p.get("education_json"):
                    try:
                        education = json.loads(next_p["education_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if next_p.get("skills_json"):
                    try:
                        skills = json.loads(next_p["skills_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if next_p.get("traits_json"):
                    try:
                        traits = json.loads(next_p["traits_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if next_p.get("score_breakdown"):
                    try:
                        score_breakdown = json.loads(next_p["score_breakdown"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                claude_analysis = None
                if next_p.get("claude_analysis"):
                    try:
                        parsed_ca = json.loads(next_p["claude_analysis"])
                        if isinstance(parsed_ca, dict):
                            claude_analysis = parsed_ca
                    except (json.JSONDecodeError, TypeError):
                        pass

                return HTMLResponse(
                    templates.env.get_template("partials/review_card.html").render(
                        {"request": request, "prospect": next_p, "experiences": experiences,
                         "education": education, "skills": skills, "traits": traits,
                         "score_breakdown": score_breakdown, "claude_analysis": claude_analysis}
                    )
                )
            else:
                return HTMLResponse("""<div class="empty-state">
                    <div class="empty-icon">&#10003;</div>
                    <p>Tous les prospects ont ete qualifies. Lance un nouveau cycle depuis la page Strategie.</p>
                </div>""")

    return updated


@router.post("/{prospect_id}/deep-screen")
async def deep_screen_single(request: Request, prospect_id: int):
    """Deep screen a single prospect: scrape LinkedIn profile, update DB, re-score."""
    import logging
    logger = logging.getLogger("okoone.deep_screen")
    scraper = request.app.state.scraper

    if not scraper:
        raise HTTPException(status_code=503, detail="Scraper non initialise")

    if not scraper._browser:
        await scraper.start()

    if not await scraper.is_session_valid():
        raise HTTPException(status_code=503, detail="Session LinkedIn expiree")

    async with get_db() as db:
        repo = ProspectRepository(db)
        prospect = await repo.find_by_id(prospect_id)
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect introuvable")

        username = prospect.get("linkedin_username")
        if not username:
            raise HTTPException(
                status_code=422,
                detail="Prospect sans linkedin_username — impossible de scraper.",
            )

        try:
            profile = await scraper.get_person_profile(username)
        except Exception as e:
            logger.error("Deep screen error for %s: %s", username, str(e)[:200])
            raise HTTPException(
                status_code=502,
                detail=f"Erreur scraping LinkedIn: {str(e)[:200]}",
            )

        if not profile:
            raise HTTPException(status_code=502, detail="Profil LinkedIn vide")

        update_data: dict = {
            "screened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for k, db_k in [
            ("full_name", "full_name"),
            ("headline", "headline"),
            ("location", "location"),
            ("about", "about_text"),
            ("current_company", "current_company"),
            ("current_title", "current_title"),
            ("profile_photo_url", "profile_photo_url"),
        ]:
            if profile.get(k):
                update_data[db_k] = profile[k]
        for k in ("experience", "education", "skills"):
            if profile.get(k):
                update_data[f"{k}_json"] = json.dumps(profile[k])

        await repo.update(prospect_id, update_data)
        await db.commit()

        logger.info(
            "Deep screened %s: %s @ %s",
            username,
            profile.get("full_name"),
            profile.get("current_company"),
        )

    # Re-score the prospect with updated data
    try:
        from app.services.prospect_service import ProspectService
        from app.services.scoring_service import ScoringService

        async with get_db() as db:
            repo = ProspectRepository(db)
            scoring = ScoringService()
            svc = ProspectService(repo, scoring)
            new_score = await svc.score_prospect(prospect_id)
            await db.commit()
            updated = await repo.find_by_id(prospect_id)
    except Exception as e:
        logger.warning("Re-scoring failed for prospect %d: %s", prospect_id, str(e)[:100])
        async with get_db() as db:
            repo = ProspectRepository(db)
            updated = await repo.find_by_id(prospect_id)

    # Web research enrichment (best-effort, don't block the response)
    try:
        from app.services.deep_analysis_service import DeepAnalysisService

        async with get_db() as db:
            repo = ProspectRepository(db)
            fresh = await repo.find_by_id(prospect_id)
            if fresh and not fresh.get("web_research_json"):
                wr_svc = DeepAnalysisService()
                wr_result = await wr_svc.web_research_prospect(fresh)
                await repo.update(prospect_id, {
                    "web_research_json": json.dumps(wr_result, ensure_ascii=False),
                })
                await db.commit()
                updated = await repo.find_by_id(prospect_id)
                logger.info("Web research completed for prospect %d", prospect_id)
    except Exception as e:
        logger.warning("Web research failed for prospect %d: %s", prospect_id, str(e)[:100])

    return updated


@router.post("/{prospect_id}/web-research")
async def web_research_single(prospect_id: int):
    """Run web research enrichment on a single prospect.

    Uses Claude CLI with web search to gather company info, funding,
    technologies, news, and social presence beyond LinkedIn.
    """
    async with get_db() as db:
        repo = ProspectRepository(db)
        prospect = await repo.find_by_id(prospect_id)
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect introuvable")

        has_experience = (
            prospect.get("experience_json")
            and prospect["experience_json"] not in ("", "[]")
        )
        if not has_experience:
            raise HTTPException(
                status_code=422,
                detail="Prospect sans donnees d'experience. Lance un deep screen d'abord.",
            )

        from app.services.deep_analysis_service import DeepAnalysisService

        svc = DeepAnalysisService()
        try:
            result = await svc.web_research_prospect(prospect)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Erreur web research: {str(exc)[:200]}",
            )

        await repo.update(prospect_id, {
            "web_research_json": json.dumps(result, ensure_ascii=False),
        })

        return result


@router.post("/deep-screen-all")
async def deep_screen_all(request: Request):
    """Deep screen all prospects that don't have experience data yet."""
    import logging
    logger = logging.getLogger("okoone.deep_screen")
    scraper = request.app.state.scraper

    if not scraper:
        return {"error": "Scraper not initialized"}

    if not scraper._browser:
        await scraper.start()

    if not await scraper.is_session_valid():
        return {"error": "LinkedIn session expired"}

    async with get_db() as db:
        repo = ProspectRepository(db)
        cursor = await db.execute(
            "SELECT id, linkedin_username FROM prospects "
            "WHERE (experience_json IS NULL OR experience_json = '' OR experience_json = '[]') "
            "AND linkedin_username IS NOT NULL "
            "ORDER BY relevance_score DESC LIMIT 50"
        )
        to_screen = await cursor.fetchall()

        screened = 0
        errors = 0
        for sp in to_screen:
            pid, username = sp[0], sp[1]
            try:
                profile = await scraper.get_person_profile(username)
                if profile:
                    update_data = {"screened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
                    for k, db_k in [("full_name", "full_name"), ("headline", "headline"),
                                     ("location", "location"), ("about", "about_text"),
                                     ("current_company", "current_company"),
                                     ("current_title", "current_title"),
                                     ("profile_photo_url", "profile_photo_url")]:
                        if profile.get(k):
                            update_data[db_k] = profile[k]
                    for k in ("experience", "education", "skills"):
                        if profile.get(k):
                            update_data[f"{k}_json"] = json.dumps(profile[k])

                    await repo.update(pid, update_data)
                    screened += 1
                    logger.info("Deep screened %s: %s @ %s", username, profile.get("full_name"), profile.get("current_company"))
            except Exception as e:
                errors += 1
                logger.error("Deep screen error for %s: %s", username, str(e)[:100])
                if "DailyLimitReached" in str(type(e).__name__):
                    break
        await db.commit()

    return {"screened": screened, "errors": errors, "total": len(to_screen)}


@router.post("/bulk-status")
async def bulk_status(request: Request):
    """Bulk update status for multiple prospects."""
    body = await request.json()
    ids = body.get("ids", [])
    new_status = body.get("status")

    if not ids or not new_status:
        raise HTTPException(status_code=400, detail="ids et status requis")

    async with get_db() as db:
        repo = ProspectRepository(db)
        for pid in ids:
            await repo.update(int(pid), {"status": new_status})

    return {"updated": len(ids), "status": new_status}
