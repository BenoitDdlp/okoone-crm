from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import aiosqlite


class ProspectRepository:
    """Raw SQL operations for the prospects table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Single-record lookups
    # ------------------------------------------------------------------

    async def find_by_username(self, username: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM prospects WHERE linkedin_username = ?", (username,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_by_id(self, prospect_id: int) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # List / paginate
    # ------------------------------------------------------------------

    async def list_all(
        self,
        status: str | None,
        min_score: float | None,
        sort_by: str,
        order: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        """Returns (prospects, total_count) for pagination."""
        allowed_sort = {
            "relevance_score",
            "created_at",
            "updated_at",
            "full_name",
            "current_company",
        }
        if sort_by not in allowed_sort:
            sort_by = "created_at"
        if order.upper() not in ("ASC", "DESC"):
            order = "DESC"

        where_clauses: list[str] = []
        params: list[str | float] = []

        if status is not None:
            where_clauses.append("status = ?")
            params.append(status)
        if min_score is not None:
            where_clauses.append("relevance_score >= ?")
            params.append(min_score)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        count_cursor = await self.db.execute(
            f"SELECT COUNT(*) FROM prospects {where_sql}", params  # noqa: S608
        )
        count_row = await count_cursor.fetchone()
        total = count_row[0] if count_row else 0

        query = (
            f"SELECT * FROM prospects {where_sql} "  # noqa: S608
            f"ORDER BY {sort_by} {order} LIMIT ? OFFSET ?"
        )
        cursor = await self.db.execute(query, [*params, limit, offset])
        rows = await cursor.fetchall()
        return [dict(r) for r in rows], total

    # ------------------------------------------------------------------
    # Create / update / upsert
    # ------------------------------------------------------------------

    async def create(self, data: dict) -> int:
        columns = list(data.keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        cursor = await self.db.execute(
            f"INSERT INTO prospects ({col_names}) VALUES ({placeholders})",  # noqa: S608
            list(data.values()),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def update(self, prospect_id: int, data: dict) -> None:
        if not data:
            return
        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join(f"{k} = ?" for k in data)
        values = list(data.values()) + [prospect_id]
        await self.db.execute(
            f"UPDATE prospects SET {set_clause} WHERE id = ?",  # noqa: S608
            values,
        )
        await self.db.commit()

    async def upsert_by_username(
        self, username: str, data: dict
    ) -> tuple[int, bool]:
        """Insert or update by linkedin_username. Returns (id, is_new)."""
        existing = await self.find_by_username(username)
        if existing:
            await self.update(existing["id"], data)
            return existing["id"], False

        data["linkedin_username"] = username
        new_id = await self.create(data)
        return new_id, True

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------

    async def search_fulltext(self, query: str, limit: int = 20) -> list[dict]:
        """Search across full_name, headline, current_company, notes."""
        like = f"%{query}%"
        cursor = await self.db.execute(
            """
            SELECT * FROM prospects
            WHERE full_name LIKE ? OR headline LIKE ?
               OR current_company LIKE ? OR notes LIKE ?
            ORDER BY relevance_score DESC
            LIMIT ?
            """,
            (like, like, like, like, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------

    async def count_by_status(self) -> dict[str, int]:
        cursor = await self.db.execute(
            "SELECT status, COUNT(*) as cnt FROM prospects GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    async def get_for_scoring(self, prospect_id: int) -> dict:
        """Get prospect with all fields needed for scoring."""
        cursor = await self.db.execute(
            "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Prospect {prospect_id} not found")
        return dict(row)

    async def get_unscored(self, limit: int = 500) -> list[dict]:
        """Get prospects that have not been scored yet."""
        cursor = await self.db.execute(
            "SELECT * FROM prospects WHERE relevance_score = 0.0 LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
