"""Unit tests for QueryMutator.

QueryMutator opens its own ``aiosqlite`` connections using a db_path, so
tests create a temporary on-disk SQLite file rather than sharing the
in-memory fixture.
"""

from __future__ import annotations

import json
import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from app.scraper.query_mutator import QueryMutator

# We reuse the schema helper from conftest
from tests.conftest import _apply_schema


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest_asyncio.fixture
async def tmp_db_path():
    """Create a temp SQLite file with the full CRM schema and yield its path."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)

    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await _apply_schema(db)
    await db.close()

    yield path

    try:
        os.unlink(path)
    except OSError:
        pass


async def _insert_search_query(
    db_path: str, keywords: str, location: str = "Singapore"
) -> int:
    """Insert a search query and return its id."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO search_queries (keywords, location, is_active) VALUES (?, ?, 1)",
            (keywords, location),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore[return-value]


async def _insert_prospect_and_review(
    db_path: str,
    username: str,
    search_id: int,
    verdict: str,
    *,
    title: str = "Engineer",
    company: str = "Acme",
    location: str = "Singapore",
    skills: list[str] | None = None,
) -> int:
    """Insert a prospect linked to a search query, plus a human review."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            INSERT INTO prospects
                (linkedin_username, full_name, current_title, current_company,
                 headline, location, source_search_id, skills_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                username.replace("-", " ").title(),
                title,
                company,
                f"{title} at {company}",
                location,
                search_id,
                json.dumps(skills or []),
            ),
        )
        prospect_id = cursor.lastrowid
        await db.execute(
            "INSERT INTO human_reviews (prospect_id, reviewer_verdict) VALUES (?, ?)",
            (prospect_id, verdict),
        )
        await db.commit()
        return prospect_id  # type: ignore[return-value]


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_evaluate_empty_query(tmp_db_path: str) -> None:
    """A query with no prospects should yield 0.0."""
    qid = await _insert_search_query(tmp_db_path, "CTO SaaS")
    mutator = QueryMutator(tmp_db_path)

    yield_rate = await mutator.evaluate_query_yield(qid)
    assert yield_rate == 0.0


@pytest.mark.asyncio
async def test_evaluate_with_reviews(tmp_db_path: str) -> None:
    """Yield should equal approved / total_reviewed."""
    qid = await _insert_search_query(tmp_db_path, "CTO SaaS")
    mutator = QueryMutator(tmp_db_path)

    # 3 approved, 2 rejected = yield 3/5 = 0.6
    for i in range(3):
        await _insert_prospect_and_review(
            tmp_db_path, f"approved-{i}", qid, "approved"
        )
    for i in range(2):
        await _insert_prospect_and_review(
            tmp_db_path, f"rejected-{i}", qid, "rejected"
        )

    yield_rate = await mutator.evaluate_query_yield(qid)
    assert abs(yield_rate - 0.6) < 0.01, f"Expected ~0.6, got {yield_rate}"


@pytest.mark.asyncio
async def test_propose_mutations(tmp_db_path: str) -> None:
    """Verify mutations are generated from approved prospect data."""
    qid = await _insert_search_query(tmp_db_path, "CTO SaaS", location="Singapore")
    mutator = QueryMutator(tmp_db_path)

    # Insert several approved prospects with shared traits
    for i in range(4):
        await _insert_prospect_and_review(
            tmp_db_path,
            f"mutation-prospect-{i}",
            qid,
            "approved",
            title="VP Engineering",
            company="TechVentures",
            location="Ho Chi Minh City",
            skills=["python", "kubernetes", "terraform"],
        )

    mutations = await mutator.propose_mutations(qid)

    assert isinstance(mutations, list)
    assert len(mutations) > 0, "Should propose at least one mutation"

    for mutation in mutations:
        assert "keywords" in mutation
        assert "location" in mutation
        assert "mutation_reason" in mutation
        # Each mutation should contain the original keywords as a prefix
        assert "CTO SaaS" in mutation["keywords"] or mutation["location"] != "Singapore"


@pytest.mark.asyncio
async def test_propose_mutations_no_approved(tmp_db_path: str) -> None:
    """If no prospects are approved, no mutations should be proposed."""
    qid = await _insert_search_query(tmp_db_path, "CTO SaaS")
    mutator = QueryMutator(tmp_db_path)

    # Only rejected
    for i in range(3):
        await _insert_prospect_and_review(
            tmp_db_path, f"only-rejected-{i}", qid, "rejected"
        )

    mutations = await mutator.propose_mutations(qid)
    assert mutations == []


@pytest.mark.asyncio
async def test_propose_mutations_nonexistent_query(tmp_db_path: str) -> None:
    """Proposing mutations for a nonexistent query should return empty list."""
    mutator = QueryMutator(tmp_db_path)
    mutations = await mutator.propose_mutations(999)
    assert mutations == []
