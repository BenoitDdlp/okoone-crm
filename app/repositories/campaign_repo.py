from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


class CampaignRepository:
    """Raw SQL operations for email campaigns, steps, enrollments, and sends."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Campaigns
    # ------------------------------------------------------------------

    async def create_campaign(self, data: dict) -> int:
        columns = list(data.keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        cursor = await self.db.execute(
            f"INSERT INTO email_campaigns ({col_names}) VALUES ({placeholders})",  # noqa: S608
            list(data.values()),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_campaign(self, campaign_id: int) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM email_campaigns WHERE id = ?", (campaign_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_campaigns(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM email_campaigns ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_campaign(self, campaign_id: int, data: dict) -> None:
        if not data:
            return
        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join(f"{k} = ?" for k in data)
        values = list(data.values()) + [campaign_id]
        await self.db.execute(
            f"UPDATE email_campaigns SET {set_clause} WHERE id = ?",  # noqa: S608
            values,
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def add_step(self, campaign_id: int, data: dict) -> int:
        data["campaign_id"] = campaign_id
        columns = list(data.keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        cursor = await self.db.execute(
            f"INSERT INTO email_steps ({col_names}) VALUES ({placeholders})",  # noqa: S608
            list(data.values()),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_steps(self, campaign_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM email_steps WHERE campaign_id = ? ORDER BY step_order ASC",
            (campaign_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Enrollments
    # ------------------------------------------------------------------

    async def enroll_prospect(self, campaign_id: int, prospect_id: int) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO email_enrollments (campaign_id, prospect_id)
            VALUES (?, ?)
            """,
            (campaign_id, prospect_id),
        )
        await self.db.commit()

        enrollment_id: int = cursor.lastrowid  # type: ignore[assignment]

        # Create the first send record automatically
        step_cursor = await self.db.execute(
            "SELECT * FROM email_steps WHERE campaign_id = ? ORDER BY step_order ASC LIMIT 1",
            (campaign_id,),
        )
        first_step = await step_cursor.fetchone()
        if first_step:
            step = dict(first_step)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            await self.db.execute(
                """
                INSERT INTO email_sends
                    (enrollment_id, step_id, prospect_id, subject, status, next_send_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    enrollment_id,
                    step["id"],
                    prospect_id,
                    step["subject_template"],
                    now,
                ),
            )
            await self.db.commit()

        return enrollment_id

    async def get_enrollments(self, campaign_id: int) -> list[dict]:
        cursor = await self.db.execute(
            """
            SELECT ee.*, p.full_name, p.linkedin_username, p.contact_email
            FROM email_enrollments ee
            JOIN prospects p ON ee.prospect_id = p.id
            WHERE ee.campaign_id = ?
            ORDER BY ee.enrolled_at DESC
            """,
            (campaign_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Sends
    # ------------------------------------------------------------------

    async def get_pending_sends(self) -> list[dict]:
        """Get email_sends where status='pending' and next_send_at <= now."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.db.execute(
            """
            SELECT es.*, p.full_name, p.contact_email, p.current_company,
                   est.body_html_template, est.body_text_template, est.subject_template,
                   ee.campaign_id
            FROM email_sends es
            JOIN email_enrollments ee ON es.enrollment_id = ee.id
            JOIN prospects p ON es.prospect_id = p.id
            JOIN email_steps est ON es.step_id = est.id
            WHERE es.status = 'pending' AND es.next_send_at <= ?
            ORDER BY es.next_send_at ASC
            """,
            (now,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_send_status(
        self,
        send_id: int,
        status: str,
        azure_id: str | None,
        error: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self.db.execute(
            """
            UPDATE email_sends
            SET status = ?, azure_message_id = ?, error_message = ?, sent_at = ?
            WHERE id = ?
            """,
            (status, azure_id, error, now, send_id),
        )
        await self.db.commit()

    async def advance_enrollment(self, enrollment_id: int) -> None:
        """Move enrollment to next step and create the next send record."""
        cursor = await self.db.execute(
            "SELECT * FROM email_enrollments WHERE id = ?", (enrollment_id,)
        )
        enrollment = await cursor.fetchone()
        if not enrollment:
            return
        enrollment = dict(enrollment)

        next_step_order = enrollment["current_step"] + 1
        step_cursor = await self.db.execute(
            "SELECT * FROM email_steps WHERE campaign_id = ? AND step_order = ?",
            (enrollment["campaign_id"], next_step_order),
        )
        next_step = await step_cursor.fetchone()

        if next_step:
            next_step = dict(next_step)
            await self.db.execute(
                "UPDATE email_enrollments SET current_step = ? WHERE id = ?",
                (next_step_order, enrollment_id),
            )
            # Schedule next send with delay
            delay_seconds = next_step["delay_days"] * 86400
            from datetime import timedelta

            send_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            await self.db.execute(
                """
                INSERT INTO email_sends
                    (enrollment_id, step_id, prospect_id, subject, status, next_send_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    enrollment_id,
                    next_step["id"],
                    enrollment["prospect_id"],
                    next_step["subject_template"],
                    send_at.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        else:
            # No more steps — mark enrollment as completed
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            await self.db.execute(
                "UPDATE email_enrollments SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, enrollment_id),
            )
        await self.db.commit()
