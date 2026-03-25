from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from datetime import datetime, timezone

import aiosqlite


class LearningService:
    """Self-improving loop: learn from human reviews to adjust scoring weights."""

    # Maximum weight adjustment per criterion per iteration (±5%)
    MAX_DELTA: float = 5.0

    async def analyze_reviews(self, db: aiosqlite.Connection) -> dict:
        """Analyze human reviews vs auto scores.

        Returns {
            current_weights: dict,
            proposed_weights: dict,
            weight_deltas: dict,
            confidence: float,
            sample_size: int,
            precision: float,
            recall: float,
        }
        """
        # 1. Load current active weights
        current_weights = await self._get_active_weights(db)

        # 2. Get all reviewed prospects with their score_breakdown
        cursor = await db.execute(
            """
            SELECT p.id, p.relevance_score, p.score_breakdown,
                   hr.reviewer_verdict, hr.relevance_override
            FROM human_reviews hr
            JOIN prospects p ON hr.prospect_id = p.id
            WHERE p.score_breakdown IS NOT NULL
            ORDER BY hr.reviewed_at DESC
            """
        )
        rows = await cursor.fetchall()
        reviews = [dict(r) for r in rows]

        sample_size = len(reviews)
        if sample_size == 0:
            return {
                "current_weights": current_weights,
                "proposed_weights": current_weights,
                "weight_deltas": {},
                "confidence": 0.0,
                "sample_size": 0,
                "precision": 0.0,
                "recall": 0.0,
            }

        # 3. Split into approved / rejected
        approved: list[dict] = []
        rejected: list[dict] = []

        for review in reviews:
            breakdown_raw = review.get("score_breakdown")
            if not breakdown_raw:
                continue
            try:
                breakdown = json.loads(breakdown_raw)
            except (json.JSONDecodeError, TypeError):
                continue

            entry = {**review, "breakdown": breakdown}
            if review["reviewer_verdict"] == "approve":
                approved.append(entry)
            elif review["reviewer_verdict"] == "reject":
                rejected.append(entry)
            # "flag" verdicts are excluded from weight learning

        if not approved and not rejected:
            return {
                "current_weights": current_weights,
                "proposed_weights": current_weights,
                "weight_deltas": {},
                "confidence": 0.0,
                "sample_size": sample_size,
                "precision": 0.0,
                "recall": 0.0,
            }

        # 4. For each scoring criterion, compute avg score for approved vs rejected
        criteria = list(current_weights.keys())
        avg_approved: dict[str, float] = {}
        avg_rejected: dict[str, float] = {}

        for criterion in criteria:
            approved_scores = [
                e["breakdown"].get(criterion, 0.0) for e in approved
                if criterion in e.get("breakdown", {})
            ]
            rejected_scores = [
                e["breakdown"].get(criterion, 0.0) for e in rejected
                if criterion in e.get("breakdown", {})
            ]

            avg_approved[criterion] = (
                statistics.mean(approved_scores) if approved_scores else 0.0
            )
            avg_rejected[criterion] = (
                statistics.mean(rejected_scores) if rejected_scores else 0.0
            )

        # 5. Compute weight adjustments
        #    If a criterion has HIGH score for rejected -> over-weighted (decrease)
        #    If a criterion has LOW score for approved -> under-weighted (increase)
        proposed_weights: dict[str, float] = {}
        weight_deltas: dict[str, float] = {}

        for criterion in criteria:
            current = current_weights.get(criterion, 0.0)
            gap = avg_approved[criterion] - avg_rejected[criterion]

            # gap > 0: criterion is discriminative (good), keep or increase
            # gap < 0: criterion is counter-productive, decrease
            # gap ~ 0: criterion is not discriminative, slight decrease

            if gap > 0.3:
                # Strong positive discrimination — increase weight
                delta = min(self.MAX_DELTA, gap * 10)
            elif gap > 0.1:
                # Moderate positive — small increase
                delta = min(self.MAX_DELTA * 0.5, gap * 5)
            elif gap > -0.1:
                # Not discriminative — no change
                delta = 0.0
            elif gap > -0.3:
                # Slightly counter-productive — small decrease
                delta = max(-self.MAX_DELTA * 0.5, gap * 5)
            else:
                # Strongly counter-productive — decrease
                delta = max(-self.MAX_DELTA, gap * 10)

            delta = round(delta, 2)
            new_weight = max(0.0, round(current + delta, 2))
            proposed_weights[criterion] = new_weight
            weight_deltas[criterion] = round(new_weight - current, 2)

        # 6. Compute precision and recall at current threshold (50.0)
        threshold = 50.0
        true_positives = sum(
            1 for e in approved if (e.get("relevance_score") or 0.0) >= threshold
        )
        false_positives = sum(
            1 for e in rejected if (e.get("relevance_score") or 0.0) >= threshold
        )
        false_negatives = sum(
            1 for e in approved if (e.get("relevance_score") or 0.0) < threshold
        )

        precision = (
            true_positives / (true_positives + false_positives)
            if (true_positives + false_positives) > 0
            else 0.0
        )
        recall = (
            true_positives / (true_positives + false_negatives)
            if (true_positives + false_negatives) > 0
            else 0.0
        )

        # Confidence based on sample size (more reviews = higher confidence)
        confidence = min(1.0, sample_size / 100.0)

        return {
            "current_weights": current_weights,
            "proposed_weights": proposed_weights,
            "weight_deltas": weight_deltas,
            "confidence": round(confidence, 3),
            "sample_size": sample_size,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
        }

    async def discover_traits(self, db: aiosqlite.Connection) -> list[dict]:
        """Find common keywords in approved prospects not captured by scoring.

        Returns list of {trait, frequency, examples}.
        """
        cursor = await db.execute(
            """
            SELECT p.headline, p.about_text, p.current_title,
                   p.current_company, p.skills_json, p.experience_json,
                   p.linkedin_username
            FROM human_reviews hr
            JOIN prospects p ON hr.prospect_id = p.id
            WHERE hr.reviewer_verdict = 'approve'
            """
        )
        rows = await cursor.fetchall()
        approved = [dict(r) for r in rows]

        if not approved:
            return []

        # Extract words from profiles
        word_counter: Counter[str] = Counter()
        word_examples: dict[str, list[str]] = {}

        # Common stop words to exclude
        stop_words = {
            "the", "and", "for", "with", "that", "this", "from", "have",
            "has", "are", "was", "were", "been", "being", "will", "would",
            "could", "should", "may", "can", "not", "but", "its", "our",
            "their", "your", "more", "than", "also", "into", "over",
            "about", "such", "which", "when", "where", "how", "all",
            "each", "every", "both", "few", "most", "other", "some",
            "new", "one", "two", "who", "what",
        }

        for prospect in approved:
            # Build text from all relevant fields
            texts = [
                prospect.get("headline") or "",
                prospect.get("about_text") or "",
                prospect.get("current_title") or "",
                prospect.get("current_company") or "",
            ]

            # Parse skills
            skills_raw = prospect.get("skills_json") or "[]"
            try:
                skills = json.loads(skills_raw) if isinstance(skills_raw, str) else skills_raw
                if isinstance(skills, list):
                    for skill in skills:
                        if isinstance(skill, str):
                            texts.append(skill)
                        elif isinstance(skill, dict):
                            texts.append(skill.get("name", ""))
            except (json.JSONDecodeError, TypeError):
                pass

            # Parse experience
            exp_raw = prospect.get("experience_json") or "[]"
            try:
                experience = json.loads(exp_raw) if isinstance(exp_raw, str) else exp_raw
                if isinstance(experience, list):
                    for exp in experience:
                        if isinstance(exp, dict):
                            texts.append(exp.get("title", ""))
                            texts.append(exp.get("company", ""))
                            texts.append(exp.get("description", ""))
            except (json.JSONDecodeError, TypeError):
                pass

            combined = " ".join(texts).lower()
            # Extract meaningful words (3+ chars, alphanumeric)
            words = re.findall(r"\b[a-z][a-z0-9]{2,}\b", combined)
            unique_words = set(words) - stop_words

            username = prospect.get("linkedin_username", "unknown")
            for word in unique_words:
                word_counter[word] += 1
                if word not in word_examples:
                    word_examples[word] = []
                if len(word_examples[word]) < 3:
                    word_examples[word].append(username)

        # Filter to words appearing in >= 30% of approved profiles
        min_frequency = max(2, int(len(approved) * 0.3))
        traits: list[dict] = []

        # Exclude words already captured by scoring constants
        from app.services.scoring_service import ScoringService

        known_keywords: set[str] = set()
        for keywords in ScoringService.SENIORITY_MAP.values():
            for kw in keywords:
                known_keywords.update(kw.lower().split())
        for title in ScoringService.TARGET_TITLES:
            known_keywords.update(title.lower().split())
        for industry in ScoringService.TARGET_INDUSTRIES:
            known_keywords.add(industry.lower())

        for word, count in word_counter.most_common(50):
            if count >= min_frequency and word not in known_keywords:
                traits.append(
                    {
                        "trait": word,
                        "frequency": count,
                        "frequency_pct": round(count / len(approved) * 100, 1),
                        "examples": word_examples.get(word, []),
                    }
                )

        return traits[:20]  # Top 20 discovered traits

    async def apply_weights(
        self, db: aiosqlite.Connection, proposed_weights: dict
    ) -> int:
        """Save new weights, create learning_signal record. Returns signal ID."""
        current_weights = await self._get_active_weights(db)

        # Deactivate current active weights
        await db.execute("UPDATE scoring_weights SET is_active = 0 WHERE is_active = 1")

        # Insert new weights
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            """
            INSERT INTO scoring_weights (name, criteria_json, is_active, created_at)
            VALUES (?, ?, 1, ?)
            """,
            (f"learned_{now.replace(' ', '_')}", json.dumps(proposed_weights), now),
        )

        # Create learning_signal record
        cursor = await db.execute(
            """
            INSERT INTO learning_signals
                (signal_type, old_value, new_value, confidence, applied_at, created_at)
            VALUES ('weight_update', ?, ?, ?, ?, ?)
            """,
            (
                json.dumps(current_weights),
                json.dumps(proposed_weights),
                1.0,
                now,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def create_eval_snapshot(self, db: aiosqlite.Connection) -> dict:
        """Compute and store evaluation metrics."""
        from app.services.eval_service import EvalService

        eval_svc = EvalService()
        metrics = await eval_svc.compute_metrics(db)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        run_id = f"eval_{now.replace(' ', '_')}"

        cursor = await db.execute(
            """
            INSERT INTO eval_snapshots
                (run_id, precision_score, recall_score, f1_score,
                 top_k_accuracy, human_agreement_rate, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                metrics["precision"],
                metrics["recall"],
                metrics["f1"],
                metrics["top_k_accuracy"],
                metrics["agreement_rate"],
                json.dumps(metrics),
                now,
            ),
        )
        await db.commit()

        return {
            "snapshot_id": cursor.lastrowid,
            "run_id": run_id,
            **metrics,
        }

    async def rollback_to_snapshot(
        self, db: aiosqlite.Connection, snapshot_id: int
    ) -> None:
        """Restore weights from a previous eval snapshot."""
        cursor = await db.execute(
            "SELECT notes FROM eval_snapshots WHERE id = ?", (snapshot_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Eval snapshot {snapshot_id} not found")

        notes_raw = row["notes"]
        try:
            snapshot_data = json.loads(notes_raw) if isinstance(notes_raw, str) else notes_raw
        except (json.JSONDecodeError, TypeError):
            raise ValueError("Snapshot does not contain restorable weight data")

        # Look for weights in the snapshot or in the learning_signals around
        # the snapshot creation time
        snapshot_cursor = await db.execute(
            "SELECT created_at FROM eval_snapshots WHERE id = ?", (snapshot_id,)
        )
        snapshot_row = await snapshot_cursor.fetchone()
        if not snapshot_row:
            raise ValueError(f"Eval snapshot {snapshot_id} not found")

        created_at = snapshot_row["created_at"]

        # Find the most recent learning signal before or at this snapshot
        signal_cursor = await db.execute(
            """
            SELECT new_value FROM learning_signals
            WHERE signal_type = 'weight_update' AND created_at <= ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (created_at,),
        )
        signal_row = await signal_cursor.fetchone()

        if not signal_row:
            # Fall back to the old_value of the first signal after snapshot
            fallback_cursor = await db.execute(
                """
                SELECT old_value FROM learning_signals
                WHERE signal_type = 'weight_update' AND created_at > ?
                ORDER BY created_at ASC LIMIT 1
                """,
                (created_at,),
            )
            fallback_row = await fallback_cursor.fetchone()
            if not fallback_row:
                raise ValueError("No weight data found for rollback")
            weights_json = fallback_row["old_value"]
        else:
            weights_json = signal_row["new_value"]

        weights = json.loads(weights_json)

        # Apply the rolled-back weights
        await self.apply_weights(db, weights)

    async def _get_active_weights(self, db: aiosqlite.Connection) -> dict:
        """Load current active scoring weights from DB."""
        cursor = await db.execute(
            "SELECT criteria_json FROM scoring_weights WHERE is_active = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row["criteria_json"])
        return {
            "title_match": 25,
            "company_fit": 20,
            "seniority": 20,
            "industry": 15,
            "location": 10,
            "completeness": 5,
            "activity": 5,
        }
