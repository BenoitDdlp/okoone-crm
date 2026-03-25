from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db

router = APIRouter(prefix="/api/v1/eval", tags=["eval"])
templates = Jinja2Templates(directory="templates")


@router.get("/metrics")
async def get_metrics(request: Request, partial: str | None = None):
    """Return current eval metrics. If partial=1, return HTML card partial."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM eval_snapshots ORDER BY created_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        latest = dict(row) if row else {}

    metrics = {
        "precision": latest.get("precision_score", 0) or 0,
        "recall": latest.get("recall_score", 0) or 0,
        "f1": latest.get("f1_score", 0) or 0,
        "agreement_rate": latest.get("human_agreement_rate", 0) or 0,
    }

    if partial == "1":
        return HTMLResponse(f"""
        <div class="metric-card">
          <div class="metric-value">{metrics['precision']:.2f}</div>
          <div class="metric-label">Precision</div>
        </div>
        <div class="metric-card">
          <div class="metric-value">{metrics['recall']:.2f}</div>
          <div class="metric-label">Rappel</div>
        </div>
        <div class="metric-card">
          <div class="metric-value">{metrics['f1']:.2f}</div>
          <div class="metric-label">F1</div>
        </div>
        <div class="metric-card">
          <div class="metric-value">{metrics['agreement_rate']:.0f}%</div>
          <div class="metric-label">Accord humain</div>
        </div>
        """)

    return metrics


@router.post("/auto-tune")
async def auto_tune():
    """Run auto-tuning of scoring weights based on human review feedback.
    Analyzes agreement between automated scores and human verdicts,
    then proposes adjusted weights.
    """
    async with get_db() as db:
        # Get current active weights
        weights_cursor = await db.execute(
            "SELECT * FROM scoring_weights WHERE is_active = 1 LIMIT 1"
        )
        weights_row = await weights_cursor.fetchone()
        if not weights_row:
            raise HTTPException(status_code=404, detail="Aucun poids actif trouve")

        current_weights = json.loads(dict(weights_row)["criteria_json"])

        # Get human reviews with corresponding prospect scores
        reviews_cursor = await db.execute(
            """
            SELECT hr.reviewer_verdict, hr.relevance_override,
                   p.relevance_score, p.score_breakdown
            FROM human_reviews hr
            JOIN prospects p ON hr.prospect_id = p.id
            ORDER BY hr.reviewed_at DESC
            LIMIT 100
            """
        )
        reviews = [dict(r) for r in await reviews_cursor.fetchall()]

        if len(reviews) < 5:
            return {
                "message": "Pas assez d'avis humains pour l'auto-tune (minimum 5)",
                "reviews_count": len(reviews),
            }

        # Simple heuristic: for approved prospects, increase weights of criteria
        # where score was high; for rejected, decrease those where score was high.
        proposed = dict(current_weights)
        approve_count = sum(1 for r in reviews if r["reviewer_verdict"] == "approve")
        reject_count = sum(1 for r in reviews if r["reviewer_verdict"] == "reject")
        total_reviews = len(reviews)

        # Nudge factor based on agreement
        agreement_rate = approve_count / total_reviews if total_reviews else 0

        for review in reviews:
            breakdown = {}
            if review.get("score_breakdown"):
                try:
                    breakdown = json.loads(review["score_breakdown"])
                except (json.JSONDecodeError, TypeError):
                    continue

            multiplier = 1.02 if review["reviewer_verdict"] == "approve" else 0.98
            for criterion in proposed:
                if criterion in breakdown:
                    proposed[criterion] = round(proposed[criterion] * multiplier, 2)

        # Normalize so sum = 100
        total_weight = sum(proposed.values())
        if total_weight > 0:
            proposed = {k: round(v / total_weight * 100, 1) for k, v in proposed.items()}

        # Compute deltas
        deltas = {}
        for k in proposed:
            deltas[k] = {
                "current": current_weights.get(k, 0),
                "proposed": proposed[k],
                "delta": round(proposed[k] - current_weights.get(k, 0), 1),
            }

    return {
        "message": "Proposition de poids generee",
        "reviews_analyzed": total_reviews,
        "agreement_rate": round(agreement_rate * 100, 1),
        "proposed_weights": proposed,
        "deltas": deltas,
    }


@router.post("/apply-weights")
async def apply_weights():
    """Apply the most recently proposed weights from auto-tune.
    Creates a new scoring_weights row and deactivates the old one.
    """
    # Re-run auto-tune to get proposed weights
    tune_result = await auto_tune()
    proposed = tune_result.get("proposed_weights")
    if not proposed:
        raise HTTPException(status_code=400, detail="Aucune proposition disponible")

    async with get_db() as db:
        # Deactivate current
        await db.execute("UPDATE scoring_weights SET is_active = 0 WHERE is_active = 1")

        # Insert new
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            """
            INSERT INTO scoring_weights (name, criteria_json, is_active, created_at)
            VALUES (?, ?, 1, ?)
            """,
            (f"auto-tune-{now}", json.dumps(proposed), now),
        )

        # Record learning signal
        await db.execute(
            """
            INSERT INTO learning_signals (signal_type, old_value, new_value, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            ("weight_update", json.dumps(tune_result.get("deltas", {})), json.dumps(proposed), now),
        )

        await db.commit()

    return {"message": "Poids appliques avec succes", "weights": proposed}


@router.get("/snapshots")
async def get_snapshots(limit: int = 20):
    """Get eval snapshots history."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM eval_snapshots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        snapshots = [dict(r) for r in await cursor.fetchall()]
    return {"snapshots": snapshots}


@router.post("/rollback/{snapshot_id}")
async def rollback(snapshot_id: int):
    """Rollback scoring weights to a previous eval snapshot.
    Finds the scoring_weights that was active at the snapshot's time and reactivates it.
    """
    async with get_db() as db:
        snap_cursor = await db.execute(
            "SELECT * FROM eval_snapshots WHERE id = ?", (snapshot_id,)
        )
        snap_row = await snap_cursor.fetchone()
        if not snap_row:
            raise HTTPException(status_code=404, detail="Snapshot introuvable")

        snapshot = dict(snap_row)

        # Find the weights that were active around the snapshot time
        weights_cursor = await db.execute(
            """
            SELECT * FROM scoring_weights
            WHERE created_at <= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (snapshot["created_at"],),
        )
        weights_row = await weights_cursor.fetchone()
        if not weights_row:
            raise HTTPException(
                status_code=404, detail="Aucun poids trouve pour cette periode"
            )

        target_weights = dict(weights_row)

        # Deactivate all, reactivate the target
        await db.execute("UPDATE scoring_weights SET is_active = 0")
        await db.execute(
            "UPDATE scoring_weights SET is_active = 1 WHERE id = ?",
            (target_weights["id"],),
        )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            """
            INSERT INTO learning_signals (signal_type, old_value, new_value, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "rollback",
                f"snapshot_{snapshot_id}",
                target_weights["criteria_json"],
                now,
            ),
        )

        await db.commit()

    return {
        "message": f"Rollback effectue vers le snapshot #{snapshot_id}",
        "restored_weights_id": target_weights["id"],
        "restored_weights_name": target_weights["name"],
    }


@router.put("/weights")
async def update_weights(request: Request):
    """Manual weight adjustment from the UI sliders."""
    body = await request.json()
    criteria_json = body.get("criteria_json")
    if not criteria_json:
        raise HTTPException(status_code=400, detail="criteria_json requis")

    # Validate JSON
    try:
        weights = json.loads(criteria_json) if isinstance(criteria_json, str) else criteria_json
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="JSON invalide")

    async with get_db() as db:
        # Get current active weights
        cursor = await db.execute(
            "SELECT * FROM scoring_weights WHERE is_active = 1 LIMIT 1"
        )
        current_row = await cursor.fetchone()
        old_json = dict(current_row)["criteria_json"] if current_row else "{}"

        # Deactivate current
        await db.execute("UPDATE scoring_weights SET is_active = 0 WHERE is_active = 1")

        # Insert new
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        weights_str = json.dumps(weights) if not isinstance(weights, str) else weights
        await db.execute(
            """
            INSERT INTO scoring_weights (name, criteria_json, is_active, created_at)
            VALUES (?, ?, 1, ?)
            """,
            (f"manual-{now}", weights_str, now),
        )

        # Learning signal
        await db.execute(
            """
            INSERT INTO learning_signals (signal_type, old_value, new_value, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            ("manual_weight_update", old_json, weights_str, now),
        )

        await db.commit()

    return {"message": "Poids mis a jour", "weights": weights}
