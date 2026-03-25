"""Autoresearch service — Karpathy-style self-improving research loop.

The loop:
1. Read current program (strategy doc) + acquaintances (positive examples)
2. Claude generates search queries from the program
3. Execute searches + profile scraping
4. Claude evaluates results against the program criteria
5. Claude proposes program improvements based on what worked/didn't
6. Human reviews proposals from the UI → approve/reject/edit
7. Repeat with improved program

The program.md is the "code" that the human steers.
Claude is the "researcher" that executes and proposes improvements.
"""

import json
from datetime import datetime

import anthropic

from app.config import settings

MODEL = "claude-sonnet-4-20250514"


class AutoresearchService:
    """Orchestrates the prospect research loop."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def _load_program(self, db) -> tuple[int, str]:
        """Load active program. Returns (version, content)."""
        async with db.execute(
            "SELECT version, content FROM prospect_program WHERE status = 'active' ORDER BY version DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0], row[1]
        return 0, ""

    async def _load_acquaintances(self, db) -> list[dict]:
        """Load all acquaintances as reference profiles."""
        async with db.execute(
            "SELECT * FROM acquaintances ORDER BY created_at DESC"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def _load_recent_results(self, db, limit: int = 30) -> str:
        """Load recent prospects with their review status for context."""
        async with db.execute("""
            SELECT p.full_name, p.headline, p.current_company, p.location,
                   p.relevance_score, p.status,
                   hr.reviewer_verdict, hr.feedback_text
            FROM prospects p
            LEFT JOIN human_reviews hr ON hr.prospect_id = p.id
            ORDER BY p.created_at DESC LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return "Aucun prospect encore."

        lines = []
        for r in rows:
            verdict = r[6] or "non-review"
            feedback = r[7] or ""
            lines.append(
                f"- {r[0]} | {r[1]} @ {r[2]} | {r[3]} | "
                f"score={r[4]} | status={r[5]} | verdict={verdict}"
                + (f" | feedback: {feedback}" if feedback else "")
            )
        return "\n".join(lines)

    async def generate_search_plan(self, db) -> list[dict]:
        """Ask Claude to generate search queries based on the program.

        Returns list of {keywords, location, reasoning}.
        """
        version, program = await self._load_program(db)
        acquaintances = await self._load_acquaintances(db)
        recent = await self._load_recent_results(db)

        acq_text = "\n".join(
            f"- {a['full_name']} — {a.get('headline', '')} @ {a.get('company', '')} "
            f"({a.get('relationship', '')}) {'[positive]' if a.get('is_positive_example') else '[negative]'}"
            for a in acquaintances
        ) if acquaintances else "Aucune acquaintance definie."

        prompt = f"""## Programme de recherche (v{version})
{program}

## Acquaintances de reference
{acq_text}

## Derniers resultats (30 recents)
{recent}

---

Genere 5-8 requetes de recherche LinkedIn variees et intelligentes basees sur le programme.
Pour chaque requete, donne:
- keywords: les mots-cles LinkedIn
- location: la localisation (ou null)
- reasoning: pourquoi cette requete est pertinente

Reponds en JSON array uniquement, pas de texte avant/apres:
[{{"keywords": "...", "location": "...", "reasoning": "..."}}]"""

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # Extract JSON from response
        if text.startswith("["):
            return json.loads(text)
        # Try to find JSON array in the text
        import re
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []

    async def evaluate_prospects(self, db, prospect_ids: list[int]) -> list[dict]:
        """Ask Claude to evaluate a batch of prospects against the program.

        Returns list of {prospect_id, score, verdict, reasoning}.
        """
        version, program = await self._load_program(db)
        acquaintances = await self._load_acquaintances(db)

        placeholders = ",".join("?" * len(prospect_ids))
        async with db.execute(
            f"SELECT * FROM prospects WHERE id IN ({placeholders})", prospect_ids
        ) as cursor:
            prospects = [dict(r) for r in await cursor.fetchall()]

        if not prospects:
            return []

        acq_text = "\n".join(
            f"- {a['full_name']} — {a.get('headline', '')} @ {a.get('company', '')}"
            for a in acquaintances[:10]
        ) if acquaintances else "Aucune."

        prospects_text = "\n\n".join(
            f"### [{p['id']}] {p.get('full_name', 'N/A')}\n"
            f"Headline: {p.get('headline', 'N/A')}\n"
            f"Company: {p.get('current_company', 'N/A')}\n"
            f"Location: {p.get('location', 'N/A')}\n"
            f"Experience: {p.get('experience_json', '[]')[:500]}\n"
            f"About: {(p.get('about_text') or '')[:300]}"
            for p in prospects
        )

        prompt = f"""## Programme (v{version})
{program}

## Acquaintances (exemples positifs)
{acq_text}

## Prospects a evaluer
{prospects_text}

---

Pour chaque prospect, evalue sa pertinence par rapport au programme.
Reponds en JSON array:
[{{"prospect_id": ID, "score": 0-100, "verdict": "qualified|maybe|reject", "reasoning": "1 phrase"}}]"""

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        import re
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []

    async def propose_program_improvement(self, db) -> dict:
        """Ask Claude to analyze results and propose an improved program.

        Returns {proposed_program, reasoning, metrics}.
        """
        version, program = await self._load_program(db)
        recent = await self._load_recent_results(db, limit=50)

        # Compute current metrics
        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN hr.reviewer_verdict = 'approve' THEN 1 ELSE 0 END) as approved,
                SUM(CASE WHEN hr.reviewer_verdict = 'reject' THEN 1 ELSE 0 END) as rejected
            FROM prospects p
            LEFT JOIN human_reviews hr ON hr.prospect_id = p.id
            WHERE p.created_at > datetime('now', '-7 days')
        """) as cursor:
            stats = dict(await cursor.fetchone())

        total = stats.get("total", 0)
        approved = stats.get("approved", 0) or 0
        qualification_rate = (approved / total * 100) if total > 0 else 0

        prompt = f"""## Programme actuel (v{version})
{program}

## Resultats des 7 derniers jours
Total prospects: {total} | Approuves: {approved} | Rejetes: {stats.get('rejected', 0)}
Taux de qualification: {qualification_rate:.0f}%

## Derniers prospects (avec verdicts)
{recent}

---

Analyse les resultats et propose une version amelioree du programme.

1. Qu'est-ce qui fonctionne bien ? (garder)
2. Qu'est-ce qui ne fonctionne pas ? (changer)
3. Quels angles sont sous-explores ? (ajouter)
4. Le programme est-il trop large ou trop restrictif ?

Puis ecris la version amelioree complete du programme (pas juste les diffs).

Reponds avec:
### Analyse
[ton analyse]

### Programme propose (v{version + 1})
```
[le programme complet ameliore]
```

### Metriques attendues
[ce que tu predis comme amelioration]"""

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text

        # Extract proposed program from code block
        import re
        program_match = re.search(r"```\n(.*?)\n```", text, re.DOTALL)
        proposed = program_match.group(1) if program_match else ""

        return {
            "analysis": text,
            "proposed_program": proposed,
            "current_version": version,
            "metrics": {
                "total": total,
                "approved": approved,
                "qualification_rate": qualification_rate,
            },
        }

    async def apply_program(self, db, new_content: str, author: str = "claude") -> int:
        """Save a new version of the program. Returns new version number."""
        version, _ = await self._load_program(db)
        new_version = version + 1

        # Deactivate old program
        await db.execute(
            "UPDATE prospect_program SET status = 'archived' WHERE status = 'active'"
        )

        # Insert new version
        await db.execute(
            "INSERT INTO prospect_program (version, content, author, parent_version) VALUES (?, ?, ?, ?)",
            (new_version, new_content, author, version),
        )
        await db.commit()
        return new_version

    async def run_full_cycle(self, db, scraper_service) -> dict:
        """Execute one full autoresearch cycle:
        1. Generate search plan from program
        2. Execute searches
        3. Evaluate new prospects
        4. Record run metrics
        5. Propose program improvement

        Returns run summary.
        """
        version, _ = await self._load_program(db)

        # Create run record
        await db.execute(
            "INSERT INTO research_runs (program_version) VALUES (?)",
            (version,),
        )
        await db.commit()
        run_id = (await db.execute("SELECT last_insert_rowid()")).fetchone()
        run_id = (await run_id)[0] if run_id else 0

        try:
            # Step 1: Generate search queries
            queries = await self.generate_search_plan(db)

            # Step 2: Execute searches (create queries in DB and run)
            new_prospect_ids: list[int] = []
            for q in queries:
                await db.execute(
                    "INSERT INTO search_queries (keywords, location) VALUES (?, ?)",
                    (q["keywords"], q.get("location")),
                )
                await db.commit()
                query_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]

                try:
                    result = await scraper_service.run_search(query_id)
                    # Collect new prospect IDs from the run
                    # (scraper_service stores them and returns stats)
                except Exception:
                    continue

            # Step 3: Get newly created prospects (from this run)
            async with db.execute(
                "SELECT id FROM prospects WHERE created_at > datetime('now', '-1 hour') AND status = 'discovered'"
            ) as cursor:
                new_prospect_ids = [row[0] for row in await cursor.fetchall()]

            # Step 4: Claude evaluates new prospects
            if new_prospect_ids:
                evaluations = await self.evaluate_prospects(db, new_prospect_ids[:20])
                for ev in evaluations:
                    await db.execute(
                        "UPDATE prospects SET relevance_score = ?, status = ? WHERE id = ?",
                        (ev["score"], "screened" if ev["verdict"] != "reject" else "rejected", ev["prospect_id"]),
                    )
                await db.commit()

            # Step 5: Propose improvement
            proposal = await self.propose_program_improvement(db)

            # Record run results
            await db.execute("""
                UPDATE research_runs SET
                    finished_at = datetime('now'),
                    status = 'completed',
                    prospects_found = ?,
                    prospects_qualified = ?,
                    metric_json = ?,
                    proposed_program = ?,
                    proposal_reasoning = ?
                WHERE id = ?
            """, (
                len(new_prospect_ids),
                sum(1 for e in (evaluations if new_prospect_ids else []) if e.get("verdict") == "qualified"),
                json.dumps(proposal.get("metrics", {})),
                proposal.get("proposed_program", ""),
                proposal.get("analysis", ""),
                run_id,
            ))
            await db.commit()

            return {
                "run_id": run_id,
                "queries_generated": len(queries),
                "prospects_found": len(new_prospect_ids),
                "proposal": proposal,
            }

        except Exception as e:
            await db.execute(
                "UPDATE research_runs SET status = 'failed', error_message = ?, finished_at = datetime('now') WHERE id = ?",
                (str(e), run_id),
            )
            await db.commit()
            raise
