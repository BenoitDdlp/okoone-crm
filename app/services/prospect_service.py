from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from app.repositories.prospect_repo import ProspectRepository
from app.services.scoring_service import ScoringService


class ProspectService:
    """Business logic layer for prospect management."""

    def __init__(
        self, repo: ProspectRepository, scoring: ScoringService
    ) -> None:
        self.repo = repo
        self.scoring = scoring

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_prospect(self, prospect_id: int) -> dict | None:
        return await self.repo.find_by_id(prospect_id)

    async def list_prospects(
        self,
        status: str | None,
        min_score: float | None,
        sort_by: str,
        order: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        return await self.repo.list_all(status, min_score, sort_by, order, limit, offset)

    async def search(self, query: str) -> list[dict]:
        return await self.repo.search_fulltext(query)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upsert_from_scrape(
        self, scraped_data: dict, search_id: int
    ) -> tuple[int, bool]:
        """Dedup by username, insert or merge, auto-score. Returns (id, is_new)."""
        username = scraped_data.get("linkedin_username", "")
        if not username:
            raise ValueError("scraped_data must contain linkedin_username")

        # Prepare the data dict for storage
        data: dict[str, str | float | int | None] = {}
        field_map = [
            "linkedin_url",
            "full_name",
            "headline",
            "location",
            "current_company",
            "current_title",
            "about_text",
            "profile_photo_url",
            "contact_email",
        ]
        for field in field_map:
            if field in scraped_data and scraped_data[field]:
                data[field] = scraped_data[field]

        # JSON fields — ensure they are stored as strings
        for json_field in ("experience_json", "education_json", "skills_json"):
            if json_field in scraped_data:
                val = scraped_data[json_field]
                if isinstance(val, (list, dict)):
                    data[json_field] = json.dumps(val)
                elif isinstance(val, str):
                    data[json_field] = val

        data["source_search_id"] = search_id
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        data["scraped_at"] = now

        prospect_id, is_new = await self.repo.upsert_by_username(username, data)

        # Auto-score the prospect
        await self._auto_score(prospect_id)

        return prospect_id, is_new

    async def update_prospect(self, prospect_id: int, data: dict) -> None:
        await self.repo.update(prospect_id, data)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    async def score_prospect(self, prospect_id: int) -> float:
        """Re-score a prospect with active weights. Returns the new score."""
        return await self._auto_score(prospect_id)

    async def score_all_unscored(self) -> int:
        """Score all prospects with relevance_score=0. Returns count scored."""
        unscored = await self.repo.get_unscored()
        count = 0
        for prospect in unscored:
            await self._auto_score(prospect["id"])
            count += 1
        return count

    async def _auto_score(self, prospect_id: int) -> float:
        """Internal: fetch prospect, load active weights, score, persist."""
        prospect = await self.repo.get_for_scoring(prospect_id)
        weights = await self._get_active_weights()

        score, breakdown = await self.scoring.score_prospect(prospect, weights)
        summary = self.scoring.generate_score_summary(breakdown, weights)

        await self.repo.update(
            prospect_id,
            {
                "relevance_score": score,
                "score_breakdown": json.dumps(breakdown),
                "score_summary": summary,
            },
        )
        return score

    async def _get_active_weights(self) -> dict:
        """Load active scoring weights from DB."""
        cursor = await self.repo.db.execute(
            "SELECT criteria_json FROM scoring_weights WHERE is_active = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row["criteria_json"])
        # Fallback defaults
        return {
            "title_match": 25,
            "company_fit": 20,
            "seniority": 20,
            "industry": 15,
            "location": 10,
            "completeness": 5,
            "activity": 5,
        }

    # ------------------------------------------------------------------
    # Dashboard stats
    # ------------------------------------------------------------------

    async def get_dashboard_stats(self) -> dict:
        """Return counts by status, avg score, total prospects, etc."""
        status_counts = await self.repo.count_by_status()

        total = sum(status_counts.values())

        # Average score
        cursor = await self.repo.db.execute(
            "SELECT AVG(relevance_score) as avg_score, "
            "MAX(relevance_score) as max_score, "
            "MIN(relevance_score) as min_score "
            "FROM prospects WHERE relevance_score > 0"
        )
        score_row = await cursor.fetchone()
        avg_score = round(score_row["avg_score"] or 0.0, 1) if score_row else 0.0
        max_score = round(score_row["max_score"] or 0.0, 1) if score_row else 0.0
        min_score = round(score_row["min_score"] or 0.0, 1) if score_row else 0.0

        # Scored vs unscored
        scored_cursor = await self.repo.db.execute(
            "SELECT COUNT(*) as cnt FROM prospects WHERE relevance_score > 0"
        )
        scored_row = await scored_cursor.fetchone()
        scored_count = scored_row["cnt"] if scored_row else 0

        # High-value prospects (score >= 60)
        hv_cursor = await self.repo.db.execute(
            "SELECT COUNT(*) as cnt FROM prospects WHERE relevance_score >= 60"
        )
        hv_row = await hv_cursor.fetchone()
        high_value_count = hv_row["cnt"] if hv_row else 0

        # Recent additions (last 7 days)
        recent_cursor = await self.repo.db.execute(
            "SELECT COUNT(*) as cnt FROM prospects "
            "WHERE created_at >= datetime('now', '-7 days')"
        )
        recent_row = await recent_cursor.fetchone()
        recent_count = recent_row["cnt"] if recent_row else 0

        return {
            "total_prospects": total,
            "status_counts": status_counts,
            "scored_count": scored_count,
            "unscored_count": total - scored_count,
            "avg_score": avg_score,
            "max_score": max_score,
            "min_score": min_score,
            "high_value_count": high_value_count,
            "recent_additions_7d": recent_count,
        }
