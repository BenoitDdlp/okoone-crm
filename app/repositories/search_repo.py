from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


class SearchRepository:
    """Raw SQL operations for search_queries and scrape_runs tables."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Search queries
    # ------------------------------------------------------------------

    async def create(self, data: dict) -> int:
        columns = list(data.keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        cursor = await self.db.execute(
            f"INSERT INTO search_queries ({col_names}) VALUES ({placeholders})",  # noqa: S608
            list(data.values()),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_active_recurring(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM search_queries WHERE is_recurring = 1 AND is_active = 1"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_last_run(self, query_id: int, total: int) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self.db.execute(
            """
            UPDATE search_queries
            SET last_run_at = ?, total_results = total_results + ?
            WHERE id = ?
            """,
            (now, total, query_id),
        )
        await self.db.commit()

    async def list_all(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM search_queries ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_by_id(self, query_id: int) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM search_queries WHERE id = ?", (query_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Scrape runs
    # ------------------------------------------------------------------

    async def create_scrape_run(self, query_id: int) -> int:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.db.execute(
            """
            INSERT INTO scrape_runs (search_query_id, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (query_id, now),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def finish_scrape_run(
        self,
        run_id: int,
        status: str,
        found: int,
        new: int,
        screened: int,
        error: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self.db.execute(
            """
            UPDATE scrape_runs
            SET finished_at = ?, status = ?, profiles_found = ?,
                profiles_new = ?, profiles_screened = ?, error_message = ?
            WHERE id = ?
            """,
            (now, status, found, new, screened, error, run_id),
        )
        await self.db.commit()

    async def get_recent_runs(self, limit: int = 20) -> list[dict]:
        cursor = await self.db.execute(
            """
            SELECT sr.*, sq.keywords, sq.location
            FROM scrape_runs sr
            LEFT JOIN search_queries sq ON sr.search_query_id = sq.id
            ORDER BY sr.started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
