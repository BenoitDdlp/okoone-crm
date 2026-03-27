"""Autoresearch service — Karpathy-style self-improving research loop.

The loop:
1. Read current program (strategy doc) + acquaintances (positive examples)
2. Claude generates search queries from the program (informed by past query performance)
3. Execute searches + profile scraping
4. Claude evaluates results against the program criteria
5. Compute cycle metrics (qualification_rate, novelty_rate, diversity_score, avg_score)
6. Claude proposes program improvements based on metrics trends from last N cycles
7. Human reviews proposals from the UI (or auto-accept if configured)
8. Repeat with improved program

The program.md is the "code" that the human steers.
Claude is the "researcher" that executes and proposes improvements.
"""

import json
import re
from datetime import datetime

from app.config import settings
from app.services.claude_advisor import _call_claude


class AutoresearchService:
    """Orchestrates the prospect research loop."""

    # ------------------------------------------------------------------ #
    # Data loaders
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Query performance tracking
    # ------------------------------------------------------------------ #

    async def _load_query_performance(self, db, limit: int = 20) -> list[dict]:
        """Load top-performing queries ranked by avg_score and qualified_count."""
        async with db.execute("""
            SELECT search_keywords, search_location,
                   SUM(prospects_found) as total_found,
                   SUM(prospects_new) as total_new,
                   AVG(avg_score) as mean_score,
                   MAX(best_score) as top_score,
                   SUM(qualified_count) as total_qualified,
                   COUNT(*) as times_used
            FROM query_performance
            GROUP BY search_keywords, search_location
            ORDER BY mean_score DESC, total_qualified DESC
            LIMIT ?
        """, (limit,)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def record_query_performance(
        self, db, keywords: str, location: str | None,
        run_id: int, prospect_ids: list[int],
    ) -> None:
        """Record how a specific query performed in terms of prospect quality."""
        if not prospect_ids:
            await db.execute(
                "INSERT INTO query_performance (search_keywords, search_location, run_id, "
                "prospects_found, prospects_new, avg_score, best_score, qualified_count) "
                "VALUES (?, ?, ?, 0, 0, 0, 0, 0)",
                (keywords, location, run_id),
            )
            return

        placeholders = ",".join("?" * len(prospect_ids))
        async with db.execute(
            f"SELECT relevance_score FROM prospects WHERE id IN ({placeholders})",
            prospect_ids,
        ) as cursor:
            scores = [row[0] or 0 for row in await cursor.fetchall()]

        avg_score = sum(scores) / len(scores) if scores else 0
        best_score = max(scores) if scores else 0
        qualified = sum(1 for s in scores if s > 50)

        await db.execute(
            "INSERT INTO query_performance (search_keywords, search_location, run_id, "
            "prospects_found, prospects_new, avg_score, best_score, qualified_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (keywords, location, run_id, len(prospect_ids), len(prospect_ids),
             avg_score, best_score, qualified),
        )

    # ------------------------------------------------------------------ #
    # Cycle metrics computation
    # ------------------------------------------------------------------ #

    async def compute_cycle_metrics(
        self, db, new_prospect_ids: list[int], evaluations: list[dict],
    ) -> dict:
        """Compute rich metrics for a single cycle.

        KEY INSIGHT (Karpathy pattern): The PRIMARY metric is the HUMAN
        approval rate, not the automatic score. Human reviews are our val_bpb.
        - If humans approve prospects → the program is working → keep
        - If humans reject prospects → the program is wrong → change

        Returns {
            qualification_rate, human_approval_rate, novelty_rate,
            diversity_score, avg_score, total_found, total_qualified,
            human_approved, human_rejected, industries, locations
        }
        """
        total_found = len(new_prospect_ids)

        # Auto-qualification rate: % of prospects scored > 50
        if evaluations:
            qualified = sum(1 for e in evaluations if (e.get("score") or 0) > 50)
            qualification_rate = (qualified / len(evaluations) * 100) if evaluations else 0
            avg_score = sum(e.get("score", 0) for e in evaluations) / len(evaluations)
        else:
            qualified = 0
            qualification_rate = 0
            avg_score = 0

        # HUMAN approval rate — THIS IS THE REAL METRIC (like val_bpb)
        # Counts all human reviews from the last 7 days
        async with db.execute("""
            SELECT
                SUM(CASE WHEN reviewer_verdict = 'approve' THEN 1 ELSE 0 END) as approved,
                SUM(CASE WHEN reviewer_verdict = 'reject' THEN 1 ELSE 0 END) as rejected,
                COUNT(*) as total_reviewed
            FROM human_reviews
            WHERE reviewed_at > datetime('now', '-7 days')
        """) as cursor:
            hr = dict(await cursor.fetchone())
        human_approved = hr.get("approved") or 0
        human_rejected = hr.get("rejected") or 0
        human_total = hr.get("total_reviewed") or 0
        human_approval_rate = (human_approved / human_total * 100) if human_total > 0 else 0

        # Novelty rate
        total_in_db = 0
        async with db.execute("SELECT COUNT(*) FROM prospects") as cursor:
            row = await cursor.fetchone()
            total_in_db = row[0] if row else 0
        novelty_rate = (total_found / total_in_db * 100) if total_in_db > 0 else 100

        # Diversity
        industries: set[str] = set()
        locations: set[str] = set()
        if new_prospect_ids:
            placeholders = ",".join("?" * len(new_prospect_ids))
            async with db.execute(
                f"SELECT headline, current_company, location FROM prospects WHERE id IN ({placeholders})",
                new_prospect_ids,
            ) as cursor:
                for row in await cursor.fetchall():
                    if row[1]:
                        industries.add(row[1].strip().lower())
                    if row[2]:
                        locations.add(row[2].strip().lower())
        diversity_score = len(industries) + len(locations)

        return {
            "qualification_rate": round(qualification_rate, 1),
            "human_approval_rate": round(human_approval_rate, 1),
            "human_approved": human_approved,
            "human_rejected": human_rejected,
            "novelty_rate": round(novelty_rate, 1),
            "diversity_score": diversity_score,
            "avg_score": round(avg_score, 1),
            "total_found": total_found,
            "total_qualified": qualified,
            "industries": sorted(industries)[:15],
            "locations": sorted(locations)[:10],
        }

    # ------------------------------------------------------------------ #
    # Metrics history loader (for improvement prompt)
    # ------------------------------------------------------------------ #

    async def _load_metrics_history(self, db, limit: int = 5) -> list[dict]:
        """Load metrics from the last N completed cycles."""
        async with db.execute("""
            SELECT id, program_version, started_at, finished_at,
                   prospects_found, prospects_qualified, metric_json,
                   proposal_status
            FROM research_runs
            WHERE status = 'completed' AND metric_json IS NOT NULL
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()

        history = []
        for r in rows:
            metrics = {}
            try:
                metrics = json.loads(r[6]) if r[6] else {}
            except (json.JSONDecodeError, TypeError):
                pass
            history.append({
                "run_id": r[0],
                "program_version": r[1],
                "started_at": r[2],
                "finished_at": r[3],
                "prospects_found": r[4],
                "prospects_qualified": r[5],
                "proposal_status": r[7],
                **metrics,
            })
        return history

    # ------------------------------------------------------------------ #
    # Search plan generation (with query performance feedback)
    # ------------------------------------------------------------------ #

    async def generate_search_plan(self, db) -> list[dict]:
        """Ask Claude to generate search queries based on the program.

        Now includes past query performance data so Claude knows what works.
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

        # Load previously used keywords to avoid duplicates
        async with db.execute(
            "SELECT keywords FROM search_queries ORDER BY created_at DESC LIMIT 50"
        ) as cursor:
            used_keywords = [row[0] for row in await cursor.fetchall()]

        used_text = "\n".join(f"- {kw}" for kw in used_keywords[:30]) if used_keywords else "Aucune (premier cycle)."

        # Load query performance data
        perf = await self._load_query_performance(db, limit=15)
        if perf:
            best_queries = [
                p for p in perf
                if p["mean_score"] and p["mean_score"] > 30
            ]
            worst_queries = [
                p for p in perf
                if p["mean_score"] is not None and p["mean_score"] <= 20 and p["times_used"] >= 1
            ]

            best_text = "\n".join(
                f"- \"{p['search_keywords']}\" @ {p['search_location'] or 'any'} → "
                f"score_moyen={p['mean_score']:.0f}, qualifies={p['total_qualified']}, "
                f"trouves={p['total_found']} (utilise {p['times_used']}x)"
                for p in best_queries[:8]
            ) if best_queries else "Pas encore de donnees significatives."

            worst_text = "\n".join(
                f"- \"{p['search_keywords']}\" @ {p['search_location'] or 'any'} → "
                f"score_moyen={p['mean_score']:.0f}, qualifies={p['total_qualified']}"
                for p in worst_queries[:5]
            ) if worst_queries else "Aucune query sous-performante identifiee."
        else:
            best_text = "Pas encore de donnees (premier cycle)."
            worst_text = "Pas encore de donnees."

        prompt = f"""## Programme de recherche (v{version})
{program}

## Acquaintances de reference
{acq_text}

## Derniers resultats (30 recents)
{recent}

## Keywords DEJA UTILISES (ne PAS les reutiliser)
{used_text}

## QUERIES LES PLUS PERFORMANTES (a s'inspirer, mais varier)
{best_text}

## QUERIES SOUS-PERFORMANTES (a eviter ou reformuler)
{worst_text}

---

## REGLES CRITIQUES POUR LA GENERATION DE QUERIES

**LinkedIn search fonctionne avec des queries COURTES et LARGES.**
La recherche LinkedIn est un moteur simple, PAS Google. Plus tu mets de mots-cles, moins tu obtiens de resultats.

### REGLE 1: Maximum 2-3 mots-cles par query
- BONNE query: "CTO Singapore" (2 mots → beaucoup de resultats)
- BONNE query: "VP Engineering Bangkok" (3 mots → bons resultats)
- BONNE query: "Head of Digital Singapore" (4 mots → acceptable)
- MAUVAISE query: "CTO proptech seed funding Jakarta Indonesia" (6 mots → 0 resultats)
- MAUVAISE query: "VP Engineering SaaS B2B fintech" (5 mots → 0 resultats)

### REGLE 2: Utilise le champ location SEPAREMENT des keywords
- CORRECT: keywords="CTO", location="Singapore" → LinkedIn filtre par geo
- INCORRECT: keywords="CTO Singapore" → cherche "CTO Singapore" dans le texte

### REGLE 3: TOUJOURS inclure au moins 2 queries pour Singapore (marche principal)
Singapore est notre marche prioritaire. Chaque plan doit avoir minimum 2 queries ciblant Singapore.

### REGLE 4: Privilegie les titres generiques
Exemples de bons keywords: "CTO", "VP Engineering", "Head of Digital", "IT Director",
"Chief Technology Officer", "Software Engineering Lead", "Director of Engineering",
"Head of Product", "VP Technology", "Digital Transformation Director"

### REGLE 5: NE JAMAIS combiner titre + secteur + stage + pays dans les keywords
Chaque mot supplementaire DIVISE le nombre de resultats par 5-10x.

Genere 5-8 requetes de recherche LinkedIn NOUVELLES et DIFFERENTES des keywords deja utilises.
Inspire-toi des queries performantes pour trouver des angles similaires mais nouveaux.
Evite les patterns des queries sous-performantes.
Varie les titres, secteurs, localisations, et formulations pour maximiser la diversite des prospects.
Pour chaque requete, donne:
- keywords: les mots-cles LinkedIn (2-3 mots MAXIMUM, DIFFERENTS des precedents)
- location: la localisation (utilise ce champ au lieu de mettre le pays dans keywords)
- reasoning: pourquoi cette requete est pertinente (reference les donnees de performance si applicable)

Reponds en JSON array uniquement, pas de texte avant/apres:
[{{"keywords": "...", "location": "...", "reasoning": "..."}}]"""

        text = (await _call_claude(prompt, system="")).strip()
        # Extract JSON from response
        if text.startswith("["):
            return json.loads(text)
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []

    # ------------------------------------------------------------------ #
    # Prospect evaluation
    # ------------------------------------------------------------------ #

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

        text = (await _call_claude(prompt, system="")).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []

    # ------------------------------------------------------------------ #
    # Program improvement (Karpathy-style: metrics-driven)
    # ------------------------------------------------------------------ #

    async def propose_program_improvement(self, db) -> dict:
        """Ask Claude to analyze metrics trends and propose an improved program.

        This is the core of the Karpathy loop: measure, compare, improve.
        Returns {proposed_program, reasoning, metrics, analysis}.
        """
        version, program = await self._load_program(db)
        recent = await self._load_recent_results(db, limit=50)

        # Load metrics from last 5 cycles
        metrics_history = await self._load_metrics_history(db, limit=5)

        # Load query performance
        query_perf = await self._load_query_performance(db, limit=15)

        # Build metrics trend text
        if metrics_history:
            metrics_text_lines = []
            for i, m in enumerate(metrics_history):
                metrics_text_lines.append(
                    f"Cycle {m.get('run_id', '?')} (v{m.get('program_version', '?')}, {m.get('started_at', '?')}):\n"
                    f"  - human_approval_rate: {m.get('human_approval_rate', 'N/A')}% (METRIQUE PRINCIPALE)\n"
                    f"  - human_approved: {m.get('human_approved', 0)} | human_rejected: {m.get('human_rejected', 0)}\n"
                    f"  - qualification_rate: {m.get('qualification_rate', 'N/A')}%\n"
                    f"  - novelty_rate: {m.get('novelty_rate', 'N/A')}%\n"
                    f"  - diversity_score: {m.get('diversity_score', 'N/A')}\n"
                    f"  - avg_score: {m.get('avg_score', 'N/A')}\n"
                    f"  - prospects_found: {m.get('prospects_found', 0)}\n"
                    f"  - prospects_qualified: {m.get('prospects_qualified', 0)}\n"
                    f"  - industries: {', '.join(m.get('industries', [])[:5]) or 'N/A'}\n"
                    f"  - locations: {', '.join(m.get('locations', [])[:5]) or 'N/A'}\n"
                    f"  - proposal_status: {m.get('proposal_status', 'N/A')}"
                )
            metrics_text = "\n\n".join(metrics_text_lines)
        else:
            metrics_text = "Aucun cycle avec metriques disponible (premier cycle)."

        # Build query performance text
        if query_perf:
            query_perf_text = "\n".join(
                f"- \"{q['search_keywords']}\" @ {q['search_location'] or 'any'} → "
                f"score_moyen={q['mean_score']:.0f}, qualifies={q['total_qualified']}/{q['total_found']}, "
                f"meilleur={q['top_score']:.0f}, utilise {q['times_used']}x"
                for q in query_perf[:15]
            )
        else:
            query_perf_text = "Aucune donnee de performance query."

        # Global stats for the last 7 days
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
        human_qual_rate = (approved / total * 100) if total > 0 else 0

        prompt = f"""## Programme actuel (v{version})
{program}

## METRIQUES DES 5 DERNIERS CYCLES (du plus recent au plus ancien)
{metrics_text}

## PERFORMANCE DES QUERIES (classees par score moyen decroissant)
{query_perf_text}

## Resultats des 7 derniers jours (reviews humaines)
Total prospects: {total} | Approuves: {approved} | Rejetes: {stats.get('rejected', 0)}
Taux de qualification humain: {human_qual_rate:.0f}%

## Derniers prospects (avec verdicts)
{recent}

---

Tu es un chercheur autonome qui ameliore iterativement un programme de recherche de prospects.
Tu suis le pattern Karpathy autoresearch: mesurer, comparer, garder/jeter, repeter.

## TA METRIQUE PRINCIPALE: le taux d'approbation humaine (human_approval_rate)

C'est l'equivalent du val_bpb de Karpathy. Quand un humain approuve un prospect, ca veut dire
que le programme fonctionne. Quand il rejette, le programme doit changer.

## Instructions d'analyse

1. **Human approval rate** (LA metrique qui compte):
   - Taux actuel: {human_qual_rate:.0f}% ({approved} approuves, {stats.get('rejected', 0)} rejetes)
   - Si < 20%: le programme est MAUVAIS, changements radicaux necessaires
   - Si 20-50%: le programme est MOYEN, ajustements cibles
   - Si > 50%: le programme est BON, optimisations fines

2. **Tendance des metriques automatiques**:
   - Le qualification_rate s'ameliore-t-il? (cible > 30%)
   - Le novelty_rate baisse-t-il? (si < 20%, on sature les memes profils)
   - Le diversity_score evolue-t-il? (plus = mieux)
   - Le avg_score progresse-t-il?

2. **Queries performantes vs sous-performantes**:
   - Quelles queries ont le meilleur score moyen et le plus de qualifies?
   - Quelles queries ne produisent rien d'utile?
   - Quels patterns communs ont les bonnes queries? (mots-cles, localisations, formulation)

3. **Angles sous-explores**:
   - Quels secteurs/localisations/titres n'apparaissent PAS dans les resultats?
   - Le programme actuel est-il trop restrictif ou trop large?
   - Quels signaux positifs manquent dans les criteres?

4. **Changements SPECIFIQUES proposes**:
   - NE PAS donner de conseils generiques ("varier les recherches")
   - Proposer des ajouts/suppressions/modifications PRECIS dans le programme
   - Proposer des types de queries specifiques a essayer
   - Ajuster les seuils si necessaire

## Format de reponse

### Analyse des tendances
[Analyse comparative des metriques cycle par cycle. Le pipeline s'ameliore-t-il?]

### Ce qui fonctionne (garder/amplifier)
[Queries, criteres, angles qui produisent des prospects qualifies]

### Ce qui ne fonctionne pas (changer/supprimer)
[Queries steriles, criteres trop larges/stricts, angles epuises]

### Angles sous-explores (ajouter)
[Nouveaux secteurs, titres, localisations, formulations a essayer]

### Programme propose (v{version + 1})
```
[Le programme complet ameliore — pas juste les diffs, mais le document entier]
```

### Predictions
[Quelles metriques devraient s'ameliorer avec ces changements et pourquoi]"""

        text = await _call_claude(prompt, system="")

        # Extract proposed program from code block
        program_match = re.search(r"```\n(.*?)\n```", text, re.DOTALL)
        proposed = program_match.group(1) if program_match else ""

        # Build current metrics summary for return value
        current_metrics = {
            "total": total,
            "approved": approved,
            "human_qualification_rate": human_qual_rate,
        }
        if metrics_history:
            latest = metrics_history[0]
            current_metrics.update({
                "qualification_rate": latest.get("qualification_rate"),
                "novelty_rate": latest.get("novelty_rate"),
                "diversity_score": latest.get("diversity_score"),
                "avg_score": latest.get("avg_score"),
            })

        return {
            "analysis": text,
            "proposed_program": proposed,
            "current_version": version,
            "metrics": current_metrics,
            "metrics_history": metrics_history,
        }

    # ------------------------------------------------------------------ #
    # Program application
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Full cycle (used by run_full_cycle, but the real loop is in jobs.py)
    # ------------------------------------------------------------------ #

    async def run_full_cycle(self, db, scraper_service) -> dict:
        """Execute one full autoresearch cycle:
        1. Generate search plan from program
        2. Execute searches
        3. Evaluate new prospects
        4. Compute and record cycle metrics
        5. Propose program improvement
        6. Optionally auto-accept improvements

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
                except Exception:
                    continue

            # Step 3: Get newly created prospects (from this run)
            async with db.execute(
                "SELECT id FROM prospects WHERE created_at > datetime('now', '-1 hour') AND status = 'discovered'"
            ) as cursor:
                new_prospect_ids = [row[0] for row in await cursor.fetchall()]

            # Step 4: Claude evaluates new prospects
            evaluations: list[dict] = []
            if new_prospect_ids:
                evaluations = await self.evaluate_prospects(db, new_prospect_ids[:20])
                for ev in evaluations:
                    await db.execute(
                        "UPDATE prospects SET relevance_score = ?, status = ? WHERE id = ?",
                        (ev["score"], "screened" if ev["verdict"] != "reject" else "rejected", ev["prospect_id"]),
                    )
                await db.commit()

            # Step 5: Compute cycle metrics
            metrics = await self.compute_cycle_metrics(db, new_prospect_ids, evaluations)

            # Step 6: Propose improvement
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
                metrics["total_qualified"],
                json.dumps(metrics),
                proposal.get("proposed_program", ""),
                proposal.get("analysis", ""),
                run_id,
            ))
            await db.commit()

            # Step 7: Auto-accept if configured
            if settings.AUTO_ACCEPT_IMPROVEMENTS and proposal.get("proposed_program"):
                await self.apply_program(db, proposal["proposed_program"], author="claude-auto")
                await db.execute(
                    "UPDATE research_runs SET proposal_status = 'auto-accepted' WHERE id = ?",
                    (run_id,),
                )
                await db.commit()

            return {
                "run_id": run_id,
                "queries_generated": len(queries),
                "prospects_found": len(new_prospect_ids),
                "metrics": metrics,
                "proposal": proposal,
            }

        except Exception as e:
            await db.execute(
                "UPDATE research_runs SET status = 'failed', error_message = ?, finished_at = datetime('now') WHERE id = ?",
                (str(e), run_id),
            )
            await db.commit()
            raise
