"""Unit tests for LearningService.

Tests cover the review analysis pipeline, weight delta capping, and
trait discovery from approved prospect profiles.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio

from app.services.learning_service import LearningService


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

async def _insert_prospect(
    db: aiosqlite.Connection,
    username: str,
    *,
    title: str = "Engineer",
    headline: str = "",
    location: str = "Singapore",
    company: str = "Acme",
    score: float = 60.0,
    breakdown: dict | None = None,
    about_text: str = "",
    skills: list[str] | None = None,
    experience: list[dict] | None = None,
) -> int:
    """Insert a prospect and return its id."""
    if breakdown is None:
        breakdown = {
            "title_match": 0.5,
            "company_fit": 0.5,
            "seniority": 0.5,
            "industry": 0.5,
            "location": 0.5,
            "completeness": 0.5,
            "activity": 0.5,
        }
    cursor = await db.execute(
        """
        INSERT INTO prospects
            (linkedin_username, full_name, headline, current_title, location,
             current_company, relevance_score, score_breakdown, about_text,
             skills_json, experience_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            username.replace("-", " ").title(),
            headline or f"{title} at {company}",
            title,
            location,
            company,
            score,
            json.dumps(breakdown),
            about_text,
            json.dumps(skills or []),
            json.dumps(experience or []),
        ),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def _insert_review(
    db: aiosqlite.Connection,
    prospect_id: int,
    verdict: str,
    override: float | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO human_reviews (prospect_id, reviewer_verdict, relevance_override)
        VALUES (?, ?, ?)
        """,
        (prospect_id, verdict, override),
    )
    await db.commit()


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_analyze_with_no_reviews(test_db) -> None:
    """With zero reviews the analysis should return empty/zero metrics."""
    svc = LearningService()
    result = await svc.analyze_reviews(test_db)

    assert result["sample_size"] == 0
    assert result["confidence"] == 0.0
    assert result["precision"] == 0.0
    assert result["recall"] == 0.0
    assert result["weight_deltas"] == {}
    # proposed_weights should equal current_weights when there is nothing to learn
    assert result["proposed_weights"] == result["current_weights"]


@pytest.mark.asyncio
async def test_analyze_with_reviews(test_db) -> None:
    """Insert prospects + reviews and verify the analysis produces weight proposals."""
    svc = LearningService()

    # Approved prospects: high title_match, high seniority
    for i in range(5):
        pid = await _insert_prospect(
            test_db,
            f"approved-{i}",
            title="CTO",
            score=75.0,
            breakdown={
                "title_match": 0.9,
                "company_fit": 0.7,
                "seniority": 0.9,
                "industry": 0.6,
                "location": 0.8,
                "completeness": 0.7,
                "activity": 0.5,
            },
        )
        await _insert_review(test_db, pid, "approve")

    # Rejected prospects: low title_match, low seniority
    for i in range(5):
        pid = await _insert_prospect(
            test_db,
            f"rejected-{i}",
            title="Intern",
            score=25.0,
            breakdown={
                "title_match": 0.1,
                "company_fit": 0.3,
                "seniority": 0.1,
                "industry": 0.2,
                "location": 0.8,
                "completeness": 0.3,
                "activity": 0.3,
            },
        )
        await _insert_review(test_db, pid, "reject")

    result = await svc.analyze_reviews(test_db)

    assert result["sample_size"] == 10
    assert result["confidence"] > 0
    assert "proposed_weights" in result
    assert "weight_deltas" in result

    # title_match and seniority should be proposed for increase (high gap)
    # because approved avg >> rejected avg
    assert result["weight_deltas"].get("title_match", 0) >= 0
    assert result["weight_deltas"].get("seniority", 0) >= 0


@pytest.mark.asyncio
async def test_weight_delta_capped(test_db) -> None:
    """Proposed weight changes should never exceed +/- MAX_DELTA (5.0)."""
    svc = LearningService()

    # Extreme divergence: approved all 1.0, rejected all 0.0
    for i in range(10):
        pid = await _insert_prospect(
            test_db,
            f"extreme-approve-{i}",
            score=90.0,
            breakdown={k: 1.0 for k in [
                "title_match", "company_fit", "seniority",
                "industry", "location", "completeness", "activity",
            ]},
        )
        await _insert_review(test_db, pid, "approve")

    for i in range(10):
        pid = await _insert_prospect(
            test_db,
            f"extreme-reject-{i}",
            score=10.0,
            breakdown={k: 0.0 for k in [
                "title_match", "company_fit", "seniority",
                "industry", "location", "completeness", "activity",
            ]},
        )
        await _insert_review(test_db, pid, "reject")

    result = await svc.analyze_reviews(test_db)

    for criterion, delta in result["weight_deltas"].items():
        assert abs(delta) <= LearningService.MAX_DELTA, (
            f"Delta for {criterion} is {delta}, exceeds MAX_DELTA={LearningService.MAX_DELTA}"
        )


@pytest.mark.asyncio
async def test_discover_traits(test_db) -> None:
    """Insert approved prospects with common keywords and verify trait discovery."""
    svc = LearningService()

    # Insert several approved prospects that share uncommon keywords
    common_skills = ["microservices", "terraform", "grafana"]
    for i in range(6):
        pid = await _insert_prospect(
            test_db,
            f"trait-prospect-{i}",
            title="Staff Engineer",
            headline=f"Staff Engineer specializing in microservices and observability",
            about_text=(
                "Experienced in microservices architecture, terraform IaC, "
                "and grafana observability stacks."
            ),
            skills=common_skills + [f"unique-skill-{i}"],
        )
        await _insert_review(test_db, pid, "approve")

    traits = await svc.discover_traits(test_db)

    assert isinstance(traits, list)
    # At least some traits should be discovered
    assert len(traits) > 0

    trait_words = [t["trait"] for t in traits]

    # "microservices" appears in all 6 headline + about + skills => should be found
    assert "microservices" in trait_words, (
        f"Expected 'microservices' in discovered traits, got {trait_words}"
    )

    # Each trait dict should have the expected shape
    for trait in traits:
        assert "trait" in trait
        assert "frequency" in trait
        assert "frequency_pct" in trait
        assert "examples" in trait
        assert trait["frequency"] >= 2
