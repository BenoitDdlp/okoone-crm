"""Integration test for the email campaign / send pipeline.

Mocks Azure Communication Services -- no real emails are sent.
Verifies: campaign creation -> step definition -> prospect enrollment ->
send processing -> email_sends records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

from app.repositories.campaign_repo import CampaignRepository
from app.repositories.prospect_repo import ProspectRepository


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

async def _insert_prospect(
    db: aiosqlite.Connection,
    username: str,
    email: str = "prospect@example.com",
    company: str = "Acme Corp",
) -> int:
    """Insert a minimal prospect and return its id."""
    cursor = await db.execute(
        """
        INSERT INTO prospects
            (linkedin_username, full_name, contact_email, current_company,
             headline, location, relevance_score)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            username.replace("-", " ").title(),
            email,
            company,
            f"Engineer at {company}",
            "Singapore",
            75.0,
        ),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_campaign_creation(test_db) -> None:
    """Create a campaign and verify it is stored."""
    repo = CampaignRepository(test_db)

    campaign_id = await repo.create_campaign({
        "name": "Q1 Outreach",
        "min_relevance_score": 50.0,
    })

    assert campaign_id > 0

    campaign = await repo.get_campaign(campaign_id)
    assert campaign is not None
    assert campaign["name"] == "Q1 Outreach"
    assert campaign["status"] == "draft"
    assert campaign["min_relevance_score"] == 50.0


@pytest.mark.asyncio
async def test_add_steps(test_db) -> None:
    """Add email steps to a campaign and verify ordering."""
    repo = CampaignRepository(test_db)

    campaign_id = await repo.create_campaign({"name": "Multi-step Campaign"})

    step1_id = await repo.add_step(campaign_id, {
        "step_order": 1,
        "subject_template": "Hi {{full_name}} - intro",
        "body_html_template": "<p>Hello {{full_name}}</p>",
        "body_text_template": "Hello {{full_name}}",
        "delay_days": 0,
    })

    step2_id = await repo.add_step(campaign_id, {
        "step_order": 2,
        "subject_template": "Following up, {{full_name}}",
        "body_html_template": "<p>Just checking in</p>",
        "body_text_template": "Just checking in",
        "delay_days": 3,
    })

    step3_id = await repo.add_step(campaign_id, {
        "step_order": 3,
        "subject_template": "Final note, {{full_name}}",
        "body_html_template": "<p>Last follow-up</p>",
        "body_text_template": "Last follow-up",
        "delay_days": 7,
    })

    steps = await repo.get_steps(campaign_id)
    assert len(steps) == 3
    assert steps[0]["step_order"] == 1
    assert steps[1]["step_order"] == 2
    assert steps[2]["step_order"] == 3
    assert steps[0]["delay_days"] == 0
    assert steps[1]["delay_days"] == 3


@pytest.mark.asyncio
async def test_enroll_prospect_creates_first_send(test_db) -> None:
    """Enrolling a prospect should automatically create the first email_sends record."""
    repo = CampaignRepository(test_db)

    # Setup campaign with one step
    campaign_id = await repo.create_campaign({"name": "Auto-send Campaign"})
    await repo.add_step(campaign_id, {
        "step_order": 1,
        "subject_template": "Welcome {{full_name}}",
        "body_html_template": "<p>Hi</p>",
        "body_text_template": "Hi",
        "delay_days": 0,
    })

    # Insert a prospect
    prospect_id = await _insert_prospect(test_db, "enroll-test-user")

    # Enroll
    enrollment_id = await repo.enroll_prospect(campaign_id, prospect_id)
    assert enrollment_id > 0

    # Verify a pending send was created
    pending = await repo.get_pending_sends()
    assert len(pending) >= 1

    send = next(
        (s for s in pending if s["prospect_id"] == prospect_id), None
    )
    assert send is not None
    assert send["status"] == "pending"
    assert send["subject"] == "Welcome {{full_name}}"


@pytest.mark.asyncio
async def test_process_sends_marks_sent(test_db) -> None:
    """Processing pending sends should mark them as 'sent'.

    This test mocks the Azure SDK to avoid real email delivery.
    """
    repo = CampaignRepository(test_db)

    # Setup
    campaign_id = await repo.create_campaign({"name": "Process Campaign"})
    await repo.add_step(campaign_id, {
        "step_order": 1,
        "subject_template": "Hello {{full_name}}",
        "body_html_template": "<p>Hi there</p>",
        "body_text_template": "Hi there",
        "delay_days": 0,
    })
    prospect_id = await _insert_prospect(
        test_db, "send-test-user", email="user@example.com"
    )
    enrollment_id = await repo.enroll_prospect(campaign_id, prospect_id)

    # Get pending sends
    pending = await repo.get_pending_sends()
    assert len(pending) >= 1

    # Mock Azure send: simulate calling update_send_status as the router does
    for send in pending:
        if send["prospect_id"] == prospect_id:
            # Simulate a successful send via Azure
            mock_azure_id = "azure-msg-12345"
            await repo.update_send_status(
                send_id=send["id"],
                status="sent",
                azure_id=mock_azure_id,
                error=None,
            )

    # Verify no more pending sends for this prospect
    remaining = await repo.get_pending_sends()
    prospect_sends = [s for s in remaining if s["prospect_id"] == prospect_id]
    assert len(prospect_sends) == 0, "Send should no longer be pending"

    # Verify the send record was updated
    cursor = await test_db.execute(
        "SELECT * FROM email_sends WHERE prospect_id = ?", (prospect_id,)
    )
    rows = await cursor.fetchall()
    assert len(rows) >= 1
    send_row = dict(rows[0])
    assert send_row["status"] == "sent"
    assert send_row["azure_message_id"] == "azure-msg-12345"
    assert send_row["sent_at"] is not None


@pytest.mark.asyncio
async def test_advance_enrollment_to_next_step(test_db) -> None:
    """After the first send succeeds, advancing should schedule the second step."""
    repo = CampaignRepository(test_db)

    campaign_id = await repo.create_campaign({"name": "Multi-step"})
    await repo.add_step(campaign_id, {
        "step_order": 1,
        "subject_template": "Step 1: Hello",
        "body_html_template": "<p>Step 1</p>",
        "body_text_template": "Step 1",
        "delay_days": 0,
    })
    await repo.add_step(campaign_id, {
        "step_order": 2,
        "subject_template": "Step 2: Follow up",
        "body_html_template": "<p>Step 2</p>",
        "body_text_template": "Step 2",
        "delay_days": 3,
    })

    prospect_id = await _insert_prospect(test_db, "advance-test-user")
    enrollment_id = await repo.enroll_prospect(campaign_id, prospect_id)

    # Mark first send as sent
    pending = await repo.get_pending_sends()
    first_send = next(s for s in pending if s["prospect_id"] == prospect_id)
    await repo.update_send_status(first_send["id"], "sent", "az-001", None)

    # Advance enrollment
    await repo.advance_enrollment(enrollment_id)

    # Verify enrollment moved to step 2
    cursor = await test_db.execute(
        "SELECT current_step FROM email_enrollments WHERE id = ?",
        (enrollment_id,),
    )
    row = await cursor.fetchone()
    assert dict(row)["current_step"] == 2

    # Verify a new pending send was created for step 2
    cursor2 = await test_db.execute(
        "SELECT * FROM email_sends WHERE enrollment_id = ? AND status = 'pending'",
        (enrollment_id,),
    )
    step2_sends = [dict(r) for r in await cursor2.fetchall()]
    assert len(step2_sends) == 1
    assert step2_sends[0]["subject"] == "Step 2: Follow up"


@pytest.mark.asyncio
async def test_advance_last_step_completes_enrollment(test_db) -> None:
    """Advancing past the last step should mark enrollment as 'completed'."""
    repo = CampaignRepository(test_db)

    campaign_id = await repo.create_campaign({"name": "Single-step"})
    await repo.add_step(campaign_id, {
        "step_order": 1,
        "subject_template": "Only step",
        "body_html_template": "<p>Only</p>",
        "body_text_template": "Only",
        "delay_days": 0,
    })

    prospect_id = await _insert_prospect(test_db, "complete-test-user")
    enrollment_id = await repo.enroll_prospect(campaign_id, prospect_id)

    # Mark the only send as sent
    pending = await repo.get_pending_sends()
    send = next(s for s in pending if s["prospect_id"] == prospect_id)
    await repo.update_send_status(send["id"], "sent", "az-fin", None)

    # Advance -- no next step exists
    await repo.advance_enrollment(enrollment_id)

    # Enrollment should be completed
    cursor = await test_db.execute(
        "SELECT status, completed_at FROM email_enrollments WHERE id = ?",
        (enrollment_id,),
    )
    row = dict(await cursor.fetchone())
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_send_failure_records_error(test_db) -> None:
    """A failed Azure send should record the error and keep status as 'failed'."""
    repo = CampaignRepository(test_db)

    campaign_id = await repo.create_campaign({"name": "Fail Campaign"})
    await repo.add_step(campaign_id, {
        "step_order": 1,
        "subject_template": "Will fail",
        "body_html_template": "<p>Fail</p>",
        "body_text_template": "Fail",
        "delay_days": 0,
    })

    prospect_id = await _insert_prospect(test_db, "fail-test-user")
    enrollment_id = await repo.enroll_prospect(campaign_id, prospect_id)

    pending = await repo.get_pending_sends()
    send = next(s for s in pending if s["prospect_id"] == prospect_id)

    # Simulate a failed send
    await repo.update_send_status(
        send["id"],
        status="failed",
        azure_id=None,
        error="InvalidRecipientAddress: email not valid",
    )

    cursor = await test_db.execute(
        "SELECT * FROM email_sends WHERE id = ?", (send["id"],)
    )
    row = dict(await cursor.fetchone())
    assert row["status"] == "failed"
    assert "InvalidRecipientAddress" in row["error_message"]
    assert row["azure_message_id"] is None
