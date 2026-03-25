"""Evolve search queries based on yield rates from human reviews.

When a human reviews scraped prospects (approve / reject), this module
analyses which queries produce high-quality leads and proposes mutations
(keyword variations, location tweaks) so the next scraping cycle focuses
on more productive search terms.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Yield thresholds
_LOW_YIELD_THRESHOLD = 0.15  # below this, consider mutating or deactivating
_DEAD_YIELD_THRESHOLD = 0.0  # exactly zero approved after enough reviews
_MIN_REVIEWS_FOR_EVAL = 5  # need at least this many reviews to judge


class QueryMutator:
    """Evolve search queries based on yield rates from human reviews."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    # -------------------------------------------------------------- #
    # Yield evaluation
    # -------------------------------------------------------------- #

    async def evaluate_query_yield(self, query_id: int) -> float:
        """Calculate yield_rate = approved / total reviewed for a search query.

        Only considers prospects that have been human-reviewed. Returns 0.0
        if no reviews exist.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            row = await db.execute_fetchall(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN hr.reviewer_verdict = 'approved' THEN 1 ELSE 0 END) AS approved
                FROM prospects p
                JOIN human_reviews hr ON hr.prospect_id = p.id
                WHERE p.source_search_id = ?
                """,
                (query_id,),
            )

        if not row or row[0]["total"] == 0:
            return 0.0

        total: int = row[0]["total"]
        approved: int = row[0]["approved"] or 0
        return approved / total

    # -------------------------------------------------------------- #
    # Mutation proposals
    # -------------------------------------------------------------- #

    async def propose_mutations(self, query_id: int) -> list[dict[str, str]]:
        """Generate mutated query variants for a given search query.

        Analyses traits from approved prospects to find patterns (common
        titles, companies, locations) and proposes new keyword combinations
        that might yield better results.

        Returns list of ``{keywords, location, mutation_reason}``.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            # Fetch the original query
            rows = await db.execute_fetchall(
                "SELECT keywords, location FROM search_queries WHERE id = ?",
                (query_id,),
            )
            if not rows:
                return []
            original_keywords: str = rows[0]["keywords"]
            original_location: str = rows[0]["location"] or ""

            # Fetch traits from approved prospects linked to this query
            approved_rows = await db.execute_fetchall(
                """
                SELECT p.headline, p.current_title, p.current_company,
                       p.location, p.traits_json, p.skills_json
                FROM prospects p
                JOIN human_reviews hr ON hr.prospect_id = p.id
                WHERE p.source_search_id = ?
                  AND hr.reviewer_verdict = 'approved'
                """,
                (query_id,),
            )

        if not approved_rows:
            return []

        mutations: list[dict[str, str]] = []

        # --- Title-based mutations ---
        title_words = Counter[str]()
        for row in approved_rows:
            title = row["current_title"] or row["headline"] or ""
            # Extract meaningful words (skip very short / common ones)
            words = [
                w.strip().lower()
                for w in re.split(r"[\s,|/&]+", title)
                if len(w.strip()) > 2 and w.strip().lower() not in _STOP_WORDS
            ]
            title_words.update(words)

        # Take the top keywords that differ from the original query
        original_lower = original_keywords.lower()
        top_title_words = [
            word
            for word, _count in title_words.most_common(10)
            if word not in original_lower
        ]

        if top_title_words:
            # Build mutation by injecting the most frequent approved-prospect title word
            for tw in top_title_words[:3]:
                mutated_kw = f"{original_keywords} {tw}"
                mutations.append(
                    {
                        "keywords": mutated_kw,
                        "location": original_location,
                        "mutation_reason": f"Added '{tw}' from approved prospect titles",
                    }
                )

        # --- Company-based mutations ---
        company_counts = Counter[str]()
        for row in approved_rows:
            company = (row["current_company"] or "").strip()
            if company and len(company) > 2:
                company_counts[company] += 1

        # If a company appears repeatedly, try adding it as a keyword
        for company, count in company_counts.most_common(2):
            if count >= 2 and company.lower() not in original_lower:
                mutations.append(
                    {
                        "keywords": f"{original_keywords} {company}",
                        "location": original_location,
                        "mutation_reason": (
                            f"Company '{company}' appeared in {count} approved prospects"
                        ),
                    }
                )

        # --- Location-based mutations ---
        location_counts = Counter[str]()
        for row in approved_rows:
            loc = (row["location"] or "").strip()
            if loc:
                location_counts[loc] += 1

        top_location = location_counts.most_common(1)
        if top_location:
            best_loc = top_location[0][0]
            if best_loc.lower() != original_location.lower():
                mutations.append(
                    {
                        "keywords": original_keywords,
                        "location": best_loc,
                        "mutation_reason": (
                            f"Location shift to '{best_loc}' "
                            f"(most common among approved prospects)"
                        ),
                    }
                )

        # --- Skill-based mutations ---
        skill_counts = Counter[str]()
        for row in approved_rows:
            try:
                skills = json.loads(row["skills_json"] or "[]")
                for s in skills:
                    s_clean = s.strip().lower()
                    if s_clean and s_clean not in original_lower and len(s_clean) > 2:
                        skill_counts[s_clean] += 1
            except (json.JSONDecodeError, TypeError):
                pass

        for skill, count in skill_counts.most_common(2):
            if count >= 2:
                mutations.append(
                    {
                        "keywords": f"{original_keywords} {skill}",
                        "location": original_location,
                        "mutation_reason": (
                            f"Skill '{skill}' common in {count} approved prospects"
                        ),
                    }
                )

        return mutations

    # -------------------------------------------------------------- #
    # Automatic application
    # -------------------------------------------------------------- #

    async def apply_best_mutations(self) -> list[int]:
        """For queries with low yield, create mutated child queries.

        Logic:
        - Queries with yield == 0 and enough reviews get deactivated.
        - Queries with yield < threshold get up to 3 mutations persisted
          as new ``search_queries`` rows (linked via ``query_mutations``).

        Returns list of newly created query IDs.
        """
        new_query_ids: list[int] = []

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            active_queries = await db.execute_fetchall(
                "SELECT id, keywords, location FROM search_queries WHERE is_active = 1"
            )

        for qrow in active_queries:
            qid: int = qrow["id"]

            # Check if there are enough reviews to evaluate
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                review_count_rows = await db.execute_fetchall(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM prospects p
                    JOIN human_reviews hr ON hr.prospect_id = p.id
                    WHERE p.source_search_id = ?
                    """,
                    (qid,),
                )
            review_count: int = review_count_rows[0]["cnt"] if review_count_rows else 0

            if review_count < _MIN_REVIEWS_FOR_EVAL:
                continue

            yield_rate = await self.evaluate_query_yield(qid)

            # Dead query: deactivate
            if yield_rate <= _DEAD_YIELD_THRESHOLD:
                logger.info(
                    "Deactivating query %d (yield=%.2f after %d reviews)",
                    qid,
                    yield_rate,
                    review_count,
                )
                async with aiosqlite.connect(self._db_path) as db:
                    await db.execute(
                        "UPDATE search_queries SET is_active = 0 WHERE id = ?",
                        (qid,),
                    )
                    await db.commit()
                continue

            # Low yield: propose and persist mutations
            if yield_rate < _LOW_YIELD_THRESHOLD:
                mutations = await self.propose_mutations(qid)
                for mutation in mutations[:3]:  # cap at 3 mutations per parent
                    async with aiosqlite.connect(self._db_path) as db:
                        cursor = await db.execute(
                            """
                            INSERT INTO search_queries
                                (keywords, location, is_recurring, is_active)
                            VALUES (?, ?, 0, 1)
                            """,
                            (mutation["keywords"], mutation["location"]),
                        )
                        new_id = cursor.lastrowid

                        # Record the mutation link
                        await db.execute(
                            """
                            INSERT INTO query_mutations
                                (parent_query_id, mutated_keywords, mutation_reason, yield_rate)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                qid,
                                mutation["keywords"],
                                mutation["mutation_reason"],
                                yield_rate,
                            ),
                        )
                        await db.commit()

                    if new_id is not None:
                        new_query_ids.append(new_id)
                        logger.info(
                            "Created mutated query %d from parent %d: '%s' (%s)",
                            new_id,
                            qid,
                            mutation["keywords"],
                            mutation["mutation_reason"],
                        )

        return new_query_ids


# ------------------------------------------------------------------ #
# Stop words for title analysis
# ------------------------------------------------------------------ #

_STOP_WORDS: set[str] = {
    "the", "and", "for", "with", "from", "this", "that", "are", "was",
    "has", "had", "have", "been", "will", "not", "but", "can", "all",
    "its", "his", "her", "our", "she", "him", "who", "what", "when",
    "how", "which", "their", "them", "than", "each", "into", "over",
    "such", "also", "very", "just", "more", "some", "only", "where",
    "most", "both", "well", "back", "own", "may", "then", "too",
    "new", "any", "about", "out", "now", "way", "get", "did",
    # LinkedIn-specific noise
    "chez", "dans", "pour", "sur", "une", "les", "des", "aux",
}
