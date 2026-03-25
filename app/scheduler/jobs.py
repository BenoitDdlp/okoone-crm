"""Background research loop — runs continuously, Karpathy-style.

The loop:
1. Read active program + acquaintances
2. Claude generates search queries
3. Scrape LinkedIn (with rate limits)
4. Claude evaluates new prospects
5. Claude proposes program improvements
6. Wait for next cycle

State is tracked in LOOP_STATE dict, visible from the UI.
"""

import logging
import traceback
from datetime import datetime

from app.database import get_db
from app.services.autoresearch_service import AutoresearchService

logger = logging.getLogger("okoone.loop")

LOOP_STATE: dict = {
    "active": False,
    "status": "idle",  # idle | searching | evaluating | proposing | sleeping | error
    "last_run_at": None,
    "last_run_result": None,
    "current_step": None,
    "cycles_completed": 0,
    "total_prospects_found": 0,
    "total_qualified": 0,
    "last_error": None,
}


async def run_research_loop():
    """One cycle of the research loop. Called by APScheduler."""
    if not LOOP_STATE["active"]:
        return

    research = AutoresearchService()

    try:
        LOOP_STATE["status"] = "searching"
        LOOP_STATE["current_step"] = "Claude genere des requetes de recherche..."

        async with get_db() as db:
            # Step 1: Generate search plan
            queries = await research.generate_search_plan(db)
            LOOP_STATE["current_step"] = f"{len(queries)} requetes generees. Scraping LinkedIn..."

            # Step 2: Execute searches (simplified — no actual scraper in this cycle,
            # we create the queries and let the scraper service handle them)
            new_prospect_ids: list[int] = []
            for i, q in enumerate(queries):
                LOOP_STATE["current_step"] = f"Recherche {i+1}/{len(queries)}: {q['keywords'][:40]}..."

                # Insert query
                cursor = await db.execute(
                    "INSERT INTO search_queries (keywords, location) VALUES (?, ?)",
                    (q["keywords"], q.get("location")),
                )
                await db.commit()

                # Note: actual LinkedIn scraping requires Patchright browser.
                # For now we record the queries. When the scraper is connected,
                # ScraperService.run_search() will execute them.

            # Step 3: Evaluate any unscored prospects
            LOOP_STATE["status"] = "evaluating"
            cursor = await db.execute("""
                SELECT id FROM prospects
                WHERE status IN ('discovered', 'screened')
                AND id NOT IN (SELECT prospect_id FROM human_reviews)
                ORDER BY created_at DESC LIMIT 20
            """)
            unreviewed_ids = [row[0] for row in await cursor.fetchall()]

            qualified_count = 0
            if unreviewed_ids:
                LOOP_STATE["current_step"] = f"Claude evalue {len(unreviewed_ids)} prospects..."
                evaluations = await research.evaluate_prospects(db, unreviewed_ids)
                for ev in evaluations:
                    new_status = "screened" if ev.get("verdict") == "qualified" else (
                        "rejected" if ev.get("verdict") == "reject" else "discovered"
                    )
                    if ev.get("verdict") == "qualified":
                        qualified_count += 1
                    await db.execute(
                        "UPDATE prospects SET relevance_score = ?, status = ? WHERE id = ?",
                        (ev.get("score", 0), new_status, ev["prospect_id"]),
                    )
                await db.commit()

            # Step 4: Propose program improvement
            LOOP_STATE["status"] = "proposing"
            LOOP_STATE["current_step"] = "Claude analyse les resultats et propose des ameliorations..."
            proposal = await research.propose_program_improvement(db)

            # Save as a research run
            version_cursor = await db.execute(
                "SELECT version FROM prospect_program WHERE status = 'active' ORDER BY version DESC LIMIT 1"
            )
            version_row = await version_cursor.fetchone()
            program_version = version_row[0] if version_row else 0

            await db.execute("""
                INSERT INTO research_runs
                    (program_version, finished_at, status, prospects_found, prospects_qualified,
                     metric_json, proposed_program, proposal_reasoning)
                VALUES (?, datetime('now'), 'completed', ?, ?, ?, ?, ?)
            """, (
                program_version,
                len(unreviewed_ids),
                qualified_count,
                "{}",
                proposal.get("proposed_program", ""),
                proposal.get("analysis", "")[:2000],
            ))
            await db.commit()

        # Update state
        LOOP_STATE["status"] = "sleeping"
        LOOP_STATE["current_step"] = "Cycle termine. Prochain cycle en attente..."
        LOOP_STATE["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        LOOP_STATE["cycles_completed"] += 1
        LOOP_STATE["total_prospects_found"] += len(unreviewed_ids)
        LOOP_STATE["total_qualified"] += qualified_count
        LOOP_STATE["last_run_result"] = {
            "queries": len(queries),
            "evaluated": len(unreviewed_ids),
            "qualified": qualified_count,
        }
        LOOP_STATE["last_error"] = None

    except Exception as e:
        LOOP_STATE["status"] = "error"
        LOOP_STATE["last_error"] = str(e)[:200]
        LOOP_STATE["current_step"] = f"Erreur: {str(e)[:100]}"
        logger.error(f"Research loop error: {traceback.format_exc()}")
