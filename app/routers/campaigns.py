from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.database import get_db
from app.models import CampaignCreate, EmailStepCreate

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])


@router.get("/")
async def list_campaigns():
    """List all email campaigns with enrollment/send counts."""
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
    return {"campaigns": campaigns}


@router.post("/")
async def create_campaign(request: Request):
    """Create a new email campaign."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nom requis")

    min_score = float(body.get("min_relevance_score", 0))

    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO email_campaigns (name, min_relevance_score)
            VALUES (?, ?)
            """,
            (name, min_score),
        )
        await db.commit()
        new_id = cursor.lastrowid

        campaign_cursor = await db.execute(
            "SELECT * FROM email_campaigns WHERE id = ?", (new_id,)
        )
        campaign = dict(await campaign_cursor.fetchone())

    return campaign


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: int):
    """Get campaign details with steps and enrollment stats."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM email_campaigns WHERE id = ?", (campaign_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Campagne introuvable")
        campaign = dict(row)

        steps_cursor = await db.execute(
            "SELECT * FROM email_steps WHERE campaign_id = ? ORDER BY step_order",
            (campaign_id,),
        )
        campaign["steps"] = [dict(r) for r in await steps_cursor.fetchall()]

        enroll_cursor = await db.execute(
            """
            SELECT ee.*, p.full_name, p.linkedin_username
            FROM email_enrollments ee
            JOIN prospects p ON ee.prospect_id = p.id
            WHERE ee.campaign_id = ?
            ORDER BY ee.enrolled_at DESC
            """,
            (campaign_id,),
        )
        campaign["enrollments"] = [dict(r) for r in await enroll_cursor.fetchall()]

    return campaign


@router.post("/{campaign_id}/steps")
async def add_step(campaign_id: int, step: EmailStepCreate):
    """Add an email step to a campaign."""
    async with get_db() as db:
        # Verify campaign exists
        cursor = await db.execute(
            "SELECT id FROM email_campaigns WHERE id = ?", (campaign_id,)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Campagne introuvable")

        await db.execute(
            """
            INSERT INTO email_steps (campaign_id, step_order, subject_template,
                                     body_html_template, body_text_template, delay_days)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                step.step_order,
                step.subject_template,
                step.body_html_template,
                step.body_text_template,
                step.delay_days,
            ),
        )
        await db.commit()

    return {"message": "Etape ajoutee", "campaign_id": campaign_id}


@router.post("/{campaign_id}/enroll/{prospect_id}")
async def enroll_prospect(campaign_id: int, prospect_id: int):
    """Enroll a prospect into a campaign."""
    async with get_db() as db:
        # Verify campaign
        camp_cursor = await db.execute(
            "SELECT id FROM email_campaigns WHERE id = ?", (campaign_id,)
        )
        if not await camp_cursor.fetchone():
            raise HTTPException(status_code=404, detail="Campagne introuvable")

        # Verify prospect
        prospect_cursor = await db.execute(
            "SELECT id FROM prospects WHERE id = ?", (prospect_id,)
        )
        if not await prospect_cursor.fetchone():
            raise HTTPException(status_code=404, detail="Prospect introuvable")

        # Check not already enrolled
        existing_cursor = await db.execute(
            "SELECT id FROM email_enrollments WHERE campaign_id = ? AND prospect_id = ?",
            (campaign_id, prospect_id),
        )
        if await existing_cursor.fetchone():
            raise HTTPException(status_code=409, detail="Deja inscrit")

        await db.execute(
            """
            INSERT INTO email_enrollments (campaign_id, prospect_id)
            VALUES (?, ?)
            """,
            (campaign_id, prospect_id),
        )
        await db.commit()

    return {"message": "Prospect inscrit", "campaign_id": campaign_id, "prospect_id": prospect_id}


@router.post("/process-sends")
async def process_sends():
    """Process pending email sends for all active campaigns.
    In production, this would be triggered by the scheduler or n8n webhook.
    """
    async with get_db() as db:
        # Find pending sends that are due
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            """
            SELECT es.*, ee.campaign_id, ee.prospect_id
            FROM email_sends es
            JOIN email_enrollments ee ON es.enrollment_id = ee.id
            WHERE es.status = 'pending' AND es.next_send_at <= ?
            ORDER BY es.next_send_at
            LIMIT 50
            """,
            (now,),
        )
        pending = [dict(r) for r in await cursor.fetchall()]

    # In production, each would be sent via Azure Communication Services.
    # Placeholder: mark as sent.
    processed = 0
    async with get_db() as db:
        for send in pending:
            await db.execute(
                "UPDATE email_sends SET status = 'sent', sent_at = ? WHERE id = ?",
                (now, send["id"]),
            )
            processed += 1
        await db.commit()

    return {"processed": processed, "message": f"{processed} emails traites"}
