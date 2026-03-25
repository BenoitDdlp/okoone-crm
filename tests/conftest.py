"""Shared fixtures for the Okoone CRM test suite.

Every test that needs a database gets an isolated in-memory SQLite connection
with the full schema already created. The ``test_app`` fixture provides an
``httpx.AsyncClient`` wired to the FastAPI app.
"""

from __future__ import annotations

import json
import os
from typing import AsyncGenerator

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Force test-safe env vars BEFORE any app import touches ``Settings()``
# ---------------------------------------------------------------------------
os.environ.setdefault("API_KEY", "test-api-key-000")
os.environ.setdefault("FERNET_KEY", "dGVzdC1mZXJuZXQta2V5LTAwMDAwMDAwMDAwMDA=")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ---------------------------------------------------------------------------
# Schema helper -- replays init_db DDL on an arbitrary connection
# ---------------------------------------------------------------------------

async def _apply_schema(db: aiosqlite.Connection) -> None:
    """Execute the same DDL statements that ``app.database.init_db`` runs."""

    await db.execute("PRAGMA journal_mode=WAL")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS search_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keywords TEXT NOT NULL,
            location TEXT,
            filters_json TEXT,
            is_recurring INTEGER DEFAULT 0,
            recurrence_cron TEXT,
            last_run_at TEXT,
            total_results INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            linkedin_username TEXT UNIQUE NOT NULL,
            linkedin_url TEXT,
            full_name TEXT,
            headline TEXT,
            location TEXT,
            current_company TEXT,
            current_title TEXT,
            experience_json TEXT,
            education_json TEXT,
            skills_json TEXT,
            about_text TEXT,
            profile_photo_url TEXT,
            contact_email TEXT,
            relevance_score REAL DEFAULT 0.0,
            score_breakdown TEXT,
            traits_json TEXT DEFAULT '[]',
            flags_json TEXT DEFAULT '[]',
            status TEXT DEFAULT 'discovered',
            source_search_id INTEGER,
            scraped_at TEXT,
            screened_at TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_query_id INTEGER REFERENCES search_queries(id),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            profiles_found INTEGER DEFAULT 0,
            profiles_new INTEGER DEFAULT 0,
            profiles_screened INTEGER DEFAULT 0,
            error_message TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS scoring_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS email_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            scoring_weight_id INTEGER,
            min_relevance_score REAL DEFAULT 0.0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS email_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER REFERENCES email_campaigns(id) ON DELETE CASCADE,
            step_order INTEGER NOT NULL,
            subject_template TEXT NOT NULL,
            body_html_template TEXT NOT NULL,
            body_text_template TEXT NOT NULL,
            delay_days INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(campaign_id, step_order)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS email_enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER REFERENCES email_campaigns(id),
            prospect_id INTEGER REFERENCES prospects(id),
            current_step INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            enrolled_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            UNIQUE(campaign_id, prospect_id)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS email_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_id INTEGER REFERENCES email_enrollments(id),
            step_id INTEGER REFERENCES email_steps(id),
            prospect_id INTEGER REFERENCES prospects(id),
            subject TEXT NOT NULL,
            sent_at TEXT,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            next_send_at TEXT,
            azure_message_id TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT NOT NULL,
            cookies_json TEXT,
            user_agent TEXT,
            is_active INTEGER DEFAULT 1,
            last_used_at TEXT,
            expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS human_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER REFERENCES prospects(id),
            reviewer_verdict TEXT NOT NULL,
            relevance_override REAL,
            feedback_text TEXT,
            reviewed_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS learning_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            confidence REAL,
            applied_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS eval_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            precision_score REAL,
            recall_score REAL,
            f1_score REAL,
            top_k_accuracy REAL,
            human_agreement_rate REAL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS query_mutations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_query_id INTEGER REFERENCES search_queries(id),
            mutated_keywords TEXT,
            mutation_reason TEXT,
            yield_rate REAL,
            avg_score_produced REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Seed the default scoring weights
    existing = await db.execute_fetchall(
        "SELECT id FROM scoring_weights WHERE name = 'default'"
    )
    if not existing:
        await db.execute(
            """
            INSERT INTO scoring_weights (name, criteria_json, is_active)
            VALUES (?, ?, 1)
            """,
            (
                "default",
                json.dumps({
                    "title_match": 25,
                    "company_fit": 20,
                    "seniority": 20,
                    "industry": 15,
                    "location": 10,
                    "completeness": 5,
                    "activity": 5,
                }),
            ),
        )

    await db.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def test_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Yield an in-memory SQLite connection with the full CRM schema."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await _apply_schema(db)
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def test_app() -> AsyncGenerator[AsyncClient, None]:
    """Yield an httpx ``AsyncClient`` bound to the FastAPI app.

    We import ``app`` late so the env-var overrides are in place first.
    """
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-API-Key": "test-api-key-000"},
    ) as client:
        yield client


@pytest.fixture
def sample_prospect() -> dict:
    """Realistic scraped prospect data."""
    return {
        "linkedin_username": "janedoe-cto",
        "linkedin_url": "https://www.linkedin.com/in/janedoe-cto/",
        "full_name": "Jane Doe",
        "headline": "CTO at TechStartup | SaaS & AI Enthusiast",
        "location": "Singapore",
        "current_company": "TechStartup Pte Ltd",
        "current_title": "CTO",
        "experience_json": json.dumps([
            {
                "company": "TechStartup Pte Ltd",
                "title": "CTO",
                "duration": "2022 - Present",
                "description": "Leading engineering for a SaaS platform.",
            },
            {
                "company": "BigCorp Inc",
                "title": "VP Engineering",
                "duration": "2019 - 2022",
                "description": "Managed 50-person engineering org.",
            },
            {
                "company": "DataLabs",
                "title": "Senior Engineer",
                "duration": "2016 - 2019",
                "description": "Built data pipelines for fintech clients.",
            },
        ]),
        "education_json": json.dumps([
            {
                "school": "National University of Singapore",
                "degree": "M.Sc.",
                "field": "Computer Science",
                "years": "2014 - 2016",
            }
        ]),
        "skills_json": json.dumps([
            "Python", "Kubernetes", "AWS", "Machine Learning",
            "System Design", "Team Leadership",
        ]),
        "about_text": (
            "Passionate technologist with 10+ years building scalable SaaS "
            "platforms in fintech and healthtech. Currently leading a 30-person "
            "engineering team at TechStartup in Singapore."
        ),
        "profile_photo_url": "https://media.licdn.com/dms/image/example/photo.jpg",
        "contact_email": "jane.doe@techstartup.sg",
    }


@pytest.fixture
def sample_search_query() -> dict:
    """Realistic search query parameters."""
    return {
        "keywords": "CTO SaaS Singapore",
        "location": "Singapore",
        "filters_json": json.dumps({"connectionOf": "1st", "industry": "Technology"}),
        "is_recurring": 1,
        "recurrence_cron": "0 9 * * 1",
    }
