"""Integration test for the scraper pipeline.

Mocks the LinkedIn scraper (no network calls) and verifies the full flow:
search query creation -> scraper service -> prospect storage with scores -> dedup.
"""

from __future__ import annotations

import json
from typing import Optional
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from app.repositories.prospect_repo import ProspectRepository
from app.repositories.search_repo import SearchRepository
from app.scraper.rate_limiter import RateLimiter
from app.services.prospect_service import ProspectService
from app.services.scoring_service import ScoringService
from app.services.scraper_service import ScraperService


# ------------------------------------------------------------------ #
# Fake scraper
# ------------------------------------------------------------------ #

def _build_fake_scraper(profiles: list[dict]) -> AsyncMock:
    """Build an ``AsyncMock`` that satisfies the ``LinkedInScraper`` protocol.

    ``search_people`` returns the supplied profiles list.
    ``get_full_profile`` returns a single profile keyed by username.
    """
    scraper = AsyncMock()
    scraper.search_people = AsyncMock(return_value=profiles)

    async def _get_full(username: str) -> dict:
        for p in profiles:
            if p.get("linkedin_username") == username:
                return p
        return {"username": username}

    scraper.get_full_profile = AsyncMock(side_effect=_get_full)
    return scraper


def _make_fake_profiles() -> list[dict]:
    """Generate a small batch of fake scraped profiles."""
    return [
        {
            "linkedin_username": "alice-cto",
            "linkedin_url": "https://www.linkedin.com/in/alice-cto/",
            "full_name": "Alice Chen",
            "headline": "CTO at FinTechGlobal | SaaS & AI",
            "location": "Singapore",
            "current_company": "FinTechGlobal",
            "current_title": "CTO",
        },
        {
            "linkedin_username": "bob-vpe",
            "linkedin_url": "https://www.linkedin.com/in/bob-vpe/",
            "full_name": "Bob Martin",
            "headline": "VP Engineering at CloudPlatform",
            "location": "Ho Chi Minh City, Vietnam",
            "current_company": "CloudPlatform",
            "current_title": "VP Engineering",
        },
        {
            "linkedin_username": "carol-intern",
            "linkedin_url": "https://www.linkedin.com/in/carol-intern/",
            "full_name": "Carol Smith",
            "headline": "Software Engineering Intern",
            "location": "London, UK",
            "current_company": "GradSchool Ltd",
            "current_title": "Intern",
        },
    ]


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest_asyncio.fixture
async def pipeline(test_db):
    """Wire up the full scraper pipeline with a mocked LinkedIn scraper."""
    fake_profiles = _make_fake_profiles()
    scraper = _build_fake_scraper(fake_profiles)

    scoring = ScoringService()
    repo = ProspectRepository(test_db)
    prospect_svc = ProspectService(repo, scoring)
    search_repo = SearchRepository(test_db)

    # Rate limiter with zero delays for test speed
    rl = RateLimiter(daily_search_limit=100, daily_profile_limit=100)
    rl._limits["search"]["min_delay"] = 0
    rl._limits["search"]["max_delay"] = 0
    rl._limits["profile"]["min_delay"] = 0
    rl._limits["profile"]["max_delay"] = 0

    svc = ScraperService(
        scraper=scraper,
        prospect_service=prospect_svc,
        search_repo=search_repo,
        rate_limiter=rl,
    )

    return svc, search_repo, repo, test_db


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_full_scraper_pipeline(pipeline) -> None:
    """Create a search query, run the scraper, verify prospects land in DB with scores."""
    svc, search_repo, prospect_repo, db = pipeline

    # Create a search query
    query_id = await search_repo.create({
        "keywords": "CTO SaaS Singapore",
        "location": "Singapore",
    })
    assert query_id > 0

    # Run the scraper pipeline
    result = await svc.run_search(query_id)

    assert result["status"] == "completed"
    assert result["profiles_found"] == 3
    assert result["profiles_new"] == 3

    # Verify prospects are in the database
    alice = await prospect_repo.find_by_username("alice-cto")
    assert alice is not None
    assert alice["full_name"] == "Alice Chen"
    assert alice["current_title"] == "CTO"
    assert alice["relevance_score"] > 0, "Prospect should have been auto-scored"

    bob = await prospect_repo.find_by_username("bob-vpe")
    assert bob is not None
    assert bob["relevance_score"] > 0

    carol = await prospect_repo.find_by_username("carol-intern")
    assert carol is not None

    # CTO in Singapore should score higher than an intern in London
    assert alice["relevance_score"] > carol["relevance_score"], (
        f"CTO score ({alice['relevance_score']}) should exceed intern "
        f"score ({carol['relevance_score']})"
    )


@pytest.mark.asyncio
async def test_dedup_across_runs(pipeline) -> None:
    """Running the same search twice should not duplicate prospects."""
    svc, search_repo, prospect_repo, db = pipeline

    query_id = await search_repo.create({
        "keywords": "CTO SaaS Singapore",
        "location": "Singapore",
    })

    # First run
    result1 = await svc.run_search(query_id)
    assert result1["profiles_new"] == 3

    # Second run (same profiles returned by mock)
    result2 = await svc.run_search(query_id)
    assert result2["profiles_found"] == 3
    assert result2["profiles_new"] == 0, (
        "Second run should produce 0 new profiles (all deduped)"
    )

    # Verify total count in DB is still 3
    all_prospects, total = await prospect_repo.list_all(
        status=None, min_score=None, sort_by="created_at",
        order="DESC", limit=100, offset=0,
    )
    assert total == 3


@pytest.mark.asyncio
async def test_scrape_run_records(pipeline) -> None:
    """Verify scrape_runs table is populated after a pipeline run."""
    svc, search_repo, prospect_repo, db = pipeline

    query_id = await search_repo.create({
        "keywords": "CTO SaaS Singapore",
        "location": "Singapore",
    })

    await svc.run_search(query_id)

    runs = await search_repo.get_recent_runs(limit=10)
    assert len(runs) >= 1

    run = runs[0]
    assert run["search_query_id"] == query_id
    assert run["status"] == "completed"
    assert run["profiles_found"] == 3
    assert run["profiles_new"] == 3


@pytest.mark.asyncio
async def test_score_breakdown_stored(pipeline) -> None:
    """Verify that score_breakdown JSON is persisted alongside the score."""
    svc, search_repo, prospect_repo, db = pipeline

    query_id = await search_repo.create({
        "keywords": "CTO SaaS",
        "location": "Singapore",
    })
    await svc.run_search(query_id)

    alice = await prospect_repo.find_by_username("alice-cto")
    assert alice is not None
    assert alice["score_breakdown"] is not None

    breakdown = json.loads(alice["score_breakdown"])
    expected_keys = {
        "title_match", "company_fit", "seniority",
        "industry", "location", "completeness", "activity",
    }
    assert set(breakdown.keys()) == expected_keys
    for key, value in breakdown.items():
        assert 0.0 <= value <= 1.0, f"{key} breakdown value out of range: {value}"
