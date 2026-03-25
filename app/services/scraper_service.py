from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Protocol

from app.repositories.search_repo import SearchRepository
from app.scraper.rate_limiter import DailyLimitReached, RateLimiter
from app.services.prospect_service import ProspectService

logger = logging.getLogger(__name__)


class LinkedInScraper(Protocol):
    """Protocol for the LinkedIn scraper implementation.

    The actual scraper will be injected at runtime. This protocol defines
    the interface that ScraperService depends on.
    """

    async def search_people(
        self, keywords: str, location: str | None, filters: dict | None
    ) -> list[dict]:
        """Search LinkedIn and return a list of basic profile dicts."""
        ...

    async def get_full_profile(self, username: str) -> dict:
        """Fetch a full profile by LinkedIn username."""
        ...


class ScraperService:
    """Orchestrates scraping pipeline: search -> dedup -> score -> store."""

    def __init__(
        self,
        scraper: LinkedInScraper,
        prospect_service: ProspectService,
        search_repo: SearchRepository,
        rate_limiter: RateLimiter,
    ) -> None:
        self.scraper = scraper
        self.prospect_service = prospect_service
        self.search_repo = search_repo
        self.rate_limiter = rate_limiter

    async def run_search(self, query_id: int) -> dict:
        """Execute a search query, scrape results, dedup, score, store.

        Returns {profiles_found, profiles_new, profiles_screened}.
        """
        query = await self.search_repo.get_by_id(query_id)
        if not query:
            raise ValueError(f"Search query {query_id} not found")

        run_id = await self.search_repo.create_scrape_run(query_id)

        profiles_found = 0
        profiles_new = 0
        profiles_screened = 0
        error_msg: str | None = None

        try:
            # Parse filters
            filters: dict | None = None
            if query.get("filters_json"):
                try:
                    filters = json.loads(query["filters_json"])
                except (json.JSONDecodeError, TypeError):
                    filters = None

            # Acquire rate limit slot for search
            await self.rate_limiter.acquire("search")

            # Execute the search
            results = await self.scraper.search_people(
                keywords=query["keywords"],
                location=query.get("location"),
                filters=filters,
            )
            profiles_found = len(results)

            # Process each result: dedup + score + store
            for profile_data in results:
                username = profile_data.get("linkedin_username", "")
                if not username:
                    continue

                try:
                    prospect_id, is_new = await self.prospect_service.upsert_from_scrape(
                        profile_data, search_id=query_id
                    )
                    if is_new:
                        profiles_new += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to upsert prospect %s: %s", username, exc
                    )
                    continue

            # Update query stats
            await self.search_repo.update_last_run(query_id, profiles_found)

            status = "completed"
        except DailyLimitReached as exc:
            error_msg = str(exc)
            status = "limit_reached"
            logger.warning("Daily limit reached during search %d: %s", query_id, exc)
        except Exception as exc:
            error_msg = str(exc)
            status = "error"
            logger.exception("Error running search %d", query_id)

        # Finalise the run record
        await self.search_repo.finish_scrape_run(
            run_id=run_id,
            status=status,
            found=profiles_found,
            new=profiles_new,
            screened=profiles_screened,
            error=error_msg,
        )

        return {
            "run_id": run_id,
            "profiles_found": profiles_found,
            "profiles_new": profiles_new,
            "profiles_screened": profiles_screened,
            "status": status,
            "error": error_msg,
        }

    async def deep_screen_prospect(self, prospect_id: int) -> dict:
        """Fetch full profile for a discovered prospect, update in DB, re-score."""
        prospect = await self.prospect_service.get_prospect(prospect_id)
        if not prospect:
            raise ValueError(f"Prospect {prospect_id} not found")

        username = prospect["linkedin_username"]

        # Rate limit the profile fetch
        await self.rate_limiter.acquire("profile")

        # Fetch the full profile
        full_profile = await self.scraper.get_full_profile(username)

        # Merge the full profile data into the prospect
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        update_data: dict[str, str | float | int | None] = {
            "screened_at": now,
            "status": "screened",
        }

        # Map full profile fields
        field_map = [
            "full_name",
            "headline",
            "location",
            "current_company",
            "current_title",
            "about_text",
            "profile_photo_url",
            "contact_email",
            "linkedin_url",
        ]
        for field in field_map:
            if field in full_profile and full_profile[field]:
                update_data[field] = full_profile[field]

        # JSON fields
        for json_field in ("experience_json", "education_json", "skills_json"):
            if json_field in full_profile:
                val = full_profile[json_field]
                if isinstance(val, (list, dict)):
                    update_data[json_field] = json.dumps(val)
                elif isinstance(val, str):
                    update_data[json_field] = val

        await self.prospect_service.update_prospect(prospect_id, update_data)

        # Re-score with enriched data
        new_score = await self.prospect_service.score_prospect(prospect_id)

        return {
            "prospect_id": prospect_id,
            "username": username,
            "new_score": new_score,
            "status": "screened",
        }

    async def run_all_recurring(self) -> list[dict]:
        """Run all active recurring search queries."""
        queries = await self.search_repo.get_active_recurring()
        results: list[dict] = []

        for query in queries:
            try:
                result = await self.run_search(query["id"])
                results.append(
                    {"query_id": query["id"], "keywords": query["keywords"], **result}
                )
            except DailyLimitReached:
                logger.warning(
                    "Daily limit reached, stopping recurring runs after %d queries",
                    len(results),
                )
                break
            except Exception as exc:
                logger.exception("Error running recurring query %d", query["id"])
                results.append(
                    {
                        "query_id": query["id"],
                        "keywords": query["keywords"],
                        "status": "error",
                        "error": str(exc),
                    }
                )

        return results
