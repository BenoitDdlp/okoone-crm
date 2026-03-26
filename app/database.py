import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiosqlite

from app.config import settings

_DB_PATH = settings.DATABASE_URL.replace("sqlite:///", "")

DEFAULT_PROGRAM = """# Prospect Research Program v1

## Objectif
Trouver des decideurs tech a Singapour et en Asie du Sud-Est qui pourraient externaliser du developpement digital aupres d'Okoone.

## Profil cible
- CTO, VP Engineering, Head of Digital, Head of Product, IT Director
- Startups en croissance (Series A-C) ou PME tech (50-500 employes)
- Secteurs: fintech, healthtech, edtech, SaaS, e-commerce, proptech
- Localisation: Singapore prioritaire, puis SEA (Vietnam, Thailand, Indonesia)

## Signaux positifs
- Recrute des developpeurs (offres ouvertes = besoin de capacite)
- Petite equipe tech relative a l'ambition produit
- Background agence/consulting (comprend le modele)
- Francophone (canal privilegie pour Okoone)
- Connexions mutuelles avec l'equipe Okoone

## Signaux negatifs
- Entreprise > 1000 employes avec grosse equipe tech interne
- Pure consulting/outsourcing (concurrent, pas client)
- Profil sans experience tech (marketing, sales purs)

## Acquaintances de reference
(Voir la table acquaintances — ces profils servent d'exemples de ce qu'on cherche)

## Strategie de recherche
1. Varier les keywords: ne pas toujours chercher "CTO Singapore"
2. Explorer les connexions de second degre des acquaintances
3. Chercher dans les entreprises qui recrutent activement des devs
4. Alterner entre recherche par role et recherche par entreprise

## Metriques de succes
- Taux de qualification: % de prospects trouves qui sont pertinents (cible > 30%)
- Diversite: ne pas sur-representer un seul secteur ou titre
- Nouveaute: % de prospects jamais vus avant (cible > 50% par run)
"""


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(_DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with get_db() as db:
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

        # --- Autoresearch tables ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prospect_program (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL DEFAULT 1,
                content TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'human',
                parent_version INTEGER,
                run_metric_json TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS acquaintances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                linkedin_url TEXT,
                headline TEXT,
                company TEXT,
                relationship TEXT,
                notes TEXT,
                is_positive_example INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS research_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                program_version INTEGER NOT NULL,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                status TEXT DEFAULT 'running',
                prospects_found INTEGER DEFAULT 0,
                prospects_qualified INTEGER DEFAULT 0,
                metric_json TEXT,
                proposed_program TEXT,
                proposal_reasoning TEXT,
                proposal_status TEXT DEFAULT 'pending',
                error_message TEXT
            )
        """)

        # --- Query performance tracking ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS query_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_keywords TEXT NOT NULL,
                search_location TEXT,
                run_id INTEGER,
                prospects_found INTEGER DEFAULT 0,
                prospects_new INTEGER DEFAULT 0,
                avg_score REAL DEFAULT 0.0,
                best_score REAL DEFAULT 0.0,
                qualified_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Seed default program if empty
        existing_program = await db.execute_fetchall(
            "SELECT id FROM prospect_program WHERE status = 'active' LIMIT 1"
        )
        if not existing_program:
            await db.execute(
                "INSERT INTO prospect_program (version, content, author) VALUES (?, ?, ?)",
                (1, DEFAULT_PROGRAM, "system"),
            )

        # Migration: add score_summary column if missing
        try:
            await db.execute("ALTER TABLE prospects ADD COLUMN score_summary TEXT")
        except Exception:
            pass  # column already exists

        # Migration: add claude_analysis column if missing
        try:
            await db.execute("ALTER TABLE prospects ADD COLUMN claude_analysis TEXT")
        except Exception:
            pass  # column already exists

        # Migration: add company_info_json column for enriched company data
        try:
            await db.execute("ALTER TABLE prospects ADD COLUMN company_info_json TEXT")
        except Exception:
            pass  # column already exists

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
                    '{"title_match": 25, "company_fit": 20, "seniority": 20, '
                    '"industry": 15, "location": 10, "completeness": 5, "activity": 5}',
                ),
            )

        await db.commit()
