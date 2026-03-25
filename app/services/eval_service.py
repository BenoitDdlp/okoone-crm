from __future__ import annotations

import json
import math

import aiosqlite


class EvalService:
    """Compute evaluation metrics for the scoring system."""

    async def compute_metrics(
        self, db: aiosqlite.Connection, score_threshold: float = 50.0
    ) -> dict:
        """Compute precision, recall, F1, top-K accuracy, human agreement rate.

        Uses human_reviews as ground truth.
        """
        cursor = await db.execute(
            """
            SELECT p.id, p.relevance_score, p.score_breakdown,
                   hr.reviewer_verdict, hr.relevance_override
            FROM human_reviews hr
            JOIN prospects p ON hr.prospect_id = p.id
            ORDER BY p.relevance_score DESC
            """
        )
        rows = await cursor.fetchall()
        reviews = [dict(r) for r in rows]

        if not reviews:
            return {
                "precision": 0.0,
                "recall": 0.0,
                "f1": 0.0,
                "top_k_accuracy": 0.0,
                "agreement_rate": 0.0,
                "sample_size": 0,
                "threshold": score_threshold,
            }

        # Classify into quadrants
        true_positives = 0   # approved AND score >= threshold
        false_positives = 0  # rejected AND score >= threshold
        true_negatives = 0   # rejected AND score < threshold
        false_negatives = 0  # approved AND score < threshold

        # For agreement rate: how often does the auto-score direction
        # match the human verdict
        agreements = 0
        total_reviews = 0

        for review in reviews:
            verdict = review["reviewer_verdict"]
            score = review.get("relevance_score") or 0.0

            # Skip "flag" verdicts — they are ambiguous
            if verdict not in ("approve", "reject"):
                continue

            total_reviews += 1
            is_positive = score >= score_threshold
            is_approved = verdict == "approve"

            if is_approved and is_positive:
                true_positives += 1
                agreements += 1
            elif not is_approved and not is_positive:
                true_negatives += 1
                agreements += 1
            elif not is_approved and is_positive:
                false_positives += 1
            else:
                false_negatives += 1

        # precision = approved_above_threshold / all_above_threshold
        precision = (
            true_positives / (true_positives + false_positives)
            if (true_positives + false_positives) > 0
            else 0.0
        )

        # recall = approved_above_threshold / all_approved
        recall = (
            true_positives / (true_positives + false_negatives)
            if (true_positives + false_negatives) > 0
            else 0.0
        )

        # F1 = harmonic mean of precision and recall
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )

        # Top-K accuracy: of the top K prospects by score, how many are approved?
        k = min(20, len(reviews))
        # Reviews are already sorted by relevance_score DESC
        top_k = reviews[:k]
        top_k_approved = sum(
            1 for r in top_k if r["reviewer_verdict"] == "approve"
        )
        top_k_accuracy = top_k_approved / k if k > 0 else 0.0

        # Agreement rate
        agreement_rate = agreements / total_reviews if total_reviews > 0 else 0.0

        return {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "top_k_accuracy": round(top_k_accuracy, 4),
            "agreement_rate": round(agreement_rate, 4),
            "sample_size": total_reviews,
            "threshold": score_threshold,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "true_negatives": true_negatives,
            "false_negatives": false_negatives,
        }

    async def get_snapshots(
        self, db: aiosqlite.Connection, limit: int = 20
    ) -> list[dict]:
        """Get eval history for the dashboard chart."""
        cursor = await db.execute(
            """
            SELECT * FROM eval_snapshots
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_scoring_comparison(self, db: aiosqlite.Connection) -> dict:
        """Side-by-side comparison of current vs last weights."""
        # Current active weights
        cursor = await db.execute(
            "SELECT * FROM scoring_weights WHERE is_active = 1 LIMIT 1"
        )
        active_row = await cursor.fetchone()

        current_weights: dict = {}
        current_name = "none"
        if active_row:
            active = dict(active_row)
            current_name = active.get("name", "unknown")
            try:
                current_weights = json.loads(active["criteria_json"])
            except (json.JSONDecodeError, TypeError):
                current_weights = {}

        # Previous weights (the most recent inactive set)
        prev_cursor = await db.execute(
            """
            SELECT * FROM scoring_weights
            WHERE is_active = 0
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        prev_row = await prev_cursor.fetchone()

        previous_weights: dict = {}
        previous_name = "none"
        if prev_row:
            prev = dict(prev_row)
            previous_name = prev.get("name", "unknown")
            try:
                previous_weights = json.loads(prev["criteria_json"])
            except (json.JSONDecodeError, TypeError):
                previous_weights = {}

        # Compute deltas
        all_criteria = set(list(current_weights.keys()) + list(previous_weights.keys()))
        comparison: list[dict] = []
        for criterion in sorted(all_criteria):
            cur = current_weights.get(criterion, 0.0)
            prev_val = previous_weights.get(criterion, 0.0)
            comparison.append(
                {
                    "criterion": criterion,
                    "current": cur,
                    "previous": prev_val,
                    "delta": round(cur - prev_val, 2),
                }
            )

        return {
            "current_name": current_name,
            "previous_name": previous_name,
            "current_weights": current_weights,
            "previous_weights": previous_weights,
            "comparison": comparison,
        }
