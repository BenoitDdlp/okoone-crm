"""Unit tests for ScoringService.

Each test exercises a single scoring dimension or the full pipeline to verify
that prospect data maps to the expected score range.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from app.services.scoring_service import ScoringService

# Default weights used across the codebase
DEFAULT_WEIGHTS: dict[str, int] = {
    "title_match": 25,
    "company_fit": 20,
    "seniority": 20,
    "industry": 15,
    "location": 10,
    "completeness": 5,
    "activity": 5,
}


@pytest.fixture
def scorer() -> ScoringService:
    return ScoringService()


# ------------------------------------------------------------------ #
# Title matching
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_score_cto_title_high(scorer: ScoringService) -> None:
    """CTO is a target title -- should yield a high title_match score."""
    prospect = {"current_title": "CTO", "headline": "CTO at StartupCo"}
    score, breakdown = await scorer.score_prospect(prospect, DEFAULT_WEIGHTS)

    assert breakdown["title_match"] == 1.0, (
        "CTO is an exact substring of a TARGET_TITLE; title_match should be 1.0"
    )


@pytest.mark.asyncio
async def test_score_junior_title_low(scorer: ScoringService) -> None:
    """Junior / intern titles should produce a low seniority score."""
    prospect = {
        "current_title": "Junior Developer",
        "headline": "Junior Software Engineer | Intern 2024",
    }
    score, breakdown = await scorer.score_prospect(prospect, DEFAULT_WEIGHTS)

    assert breakdown["seniority"] <= 0.15, (
        f"Seniority for junior/intern should be <= 0.15, got {breakdown['seniority']}"
    )


# ------------------------------------------------------------------ #
# Location scoring
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_score_singapore_location(scorer: ScoringService) -> None:
    """Singapore is the top-tier target location, score should be 1.0."""
    prospect = {"location": "Singapore"}
    _score, breakdown = await scorer.score_prospect(prospect, DEFAULT_WEIGHTS)

    assert breakdown["location"] == 1.0


@pytest.mark.asyncio
async def test_score_france_location(scorer: ScoringService) -> None:
    """France is in the Western tier (0.3)."""
    prospect = {"location": "Paris, France"}
    _score, breakdown = await scorer.score_prospect(prospect, DEFAULT_WEIGHTS)

    assert breakdown["location"] == 0.3, (
        f"France should map to 0.3, got {breakdown['location']}"
    )


# ------------------------------------------------------------------ #
# Completeness scoring
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_score_complete_profile(
    scorer: ScoringService, sample_prospect: dict
) -> None:
    """A fully filled profile should have completeness near 1.0."""
    _score, breakdown = await scorer.score_prospect(sample_prospect, DEFAULT_WEIGHTS)

    # sample_prospect has email, about, experience, education, skills, photo
    assert breakdown["completeness"] >= 0.8, (
        f"Complete profile should score >= 0.8, got {breakdown['completeness']}"
    )


@pytest.mark.asyncio
async def test_score_empty_profile(scorer: ScoringService) -> None:
    """A bare profile with no enriched fields should have low completeness."""
    prospect = {
        "linkedin_username": "empty-user",
        "current_title": "",
        "headline": "",
    }
    _score, breakdown = await scorer.score_prospect(prospect, DEFAULT_WEIGHTS)

    assert breakdown["completeness"] <= 0.2, (
        f"Empty profile should score <= 0.2, got {breakdown['completeness']}"
    )


# ------------------------------------------------------------------ #
# Full pipeline
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_full_scoring_pipeline(
    scorer: ScoringService, sample_prospect: dict
) -> None:
    """End-to-end scoring should produce a value in [0, 100]."""
    score, breakdown = await scorer.score_prospect(sample_prospect, DEFAULT_WEIGHTS)

    assert 0.0 <= score <= 100.0, f"Score should be in [0,100], got {score}"
    # With a strong CTO / Singapore / SaaS profile the score should be high
    assert score >= 50.0, (
        f"A strong CTO prospect should score >= 50, got {score}"
    )
    # All breakdown keys should be present
    for key in DEFAULT_WEIGHTS:
        assert key in breakdown, f"Missing breakdown key: {key}"


@pytest.mark.asyncio
async def test_custom_weights(scorer: ScoringService) -> None:
    """Different weight distributions should produce different scores.

    We use a prospect whose scores diverge sharply across criteria so
    that shifting weights from one dimension to another changes the total.
    """
    # Prospect: great title (CTO) but unknown/low location
    prospect = {
        "current_title": "CTO",
        "headline": "CTO at SmallCo",
        "location": "Nairobi, Kenya",  # not in any tier => 0.1
        "current_company": "SmallCo",
        "experience_json": "[]",
        "education_json": "[]",
        "skills_json": "[]",
    }

    # Weight set A: heavily favours location
    weights_a = {
        "title_match": 5,
        "company_fit": 5,
        "seniority": 5,
        "industry": 5,
        "location": 70,
        "completeness": 5,
        "activity": 5,
    }

    # Weight set B: heavily favours title
    weights_b = {
        "title_match": 70,
        "company_fit": 5,
        "seniority": 5,
        "industry": 5,
        "location": 5,
        "completeness": 5,
        "activity": 5,
    }

    score_a, _ = await scorer.score_prospect(prospect, weights_a)
    score_b, _ = await scorer.score_prospect(prospect, weights_b)

    # Both should be valid scores
    assert 0 <= score_a <= 100
    assert 0 <= score_b <= 100

    # With location=0.1 weighted heavily (A) vs title=1.0 weighted heavily (B),
    # the scores must differ.
    assert score_a != score_b, (
        f"Different weights should produce different scores: A={score_a}, B={score_b}"
    )
    # Title-heavy should score higher because CTO = 1.0 vs location = 0.1
    assert score_b > score_a, (
        f"Title-heavy score ({score_b}) should exceed location-heavy ({score_a})"
    )
