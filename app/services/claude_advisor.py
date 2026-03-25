"""Claude-powered self-improving advisor for prospect screening.

Orchestrates the learning loop via the Claude CLI (`claude -p`).
Uses the CLI instead of the Python SDK because the VPS authenticates
with an OAuth token (sk-ant-oat01-*) managed by the Claude CLI.
"""

import asyncio
import json
import shutil
from datetime import datetime

from app.config import settings

CLAUDE_CLI = settings.CLAUDE_CLI_PATH or shutil.which("claude") or "claude"
MODEL = settings.CLAUDE_MODEL or "claude-opus-4-6"

SYSTEM_PROMPT = """Tu es l'assistant IA du CRM de prospection Okoone. Tu aides a ameliorer le screening
de prospects LinkedIn pour une agence digitale basee en Asie du Sud-Est.

Tu as acces au contexte suivant a chaque message:
- Les poids de scoring actuels et leur performance
- Les derniers prospects approuves/rejetes avec leur score breakdown
- Les requetes de recherche LinkedIn actives et leur yield rate
- Les metriques d'evaluation (precision, rappel, F1, accord humain)

Ton role:
1. ANALYSER les patterns dans les reviews humaines (pourquoi certains prospects sont approuves/rejetes)
2. PROPOSER des ajustements concrets: nouveaux poids, nouvelles queries, nouveaux traits a detecter
3. REPONDRE aux questions sur la qualite du pipeline et suggerer des ameliorations
4. GENERER des insights non-evidents (correlations, angles morts, opportunites)

Reponds en francais. Sois concis et actionnable. Chaque reponse doit contenir au moins une suggestion concrete.
Quand tu proposes des changements de poids, utilise le format JSON: {"weight_name": new_value, ...}
Quand tu proposes des queries, utilise le format: {"keywords": "...", "location": "..."}"""


def _claude_env() -> dict[str, str]:
    """Build a clean env for the Claude CLI subprocess."""
    import os
    home = os.environ.get("HOME", "/home/openclaw")
    return {
        "HOME": home,
        "PATH": f"{home}/.npm-global/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "NODE_PATH": f"{home}/.npm-global/lib/node_modules",
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    }


async def _call_claude(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """Call Claude via CLI, passing the prompt via stdin (supports long prompts)."""
    full_prompt = f"{system}\n\n---\n\n{prompt}" if system else prompt
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_CLI, "-p", "-", "--model", MODEL,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_claude_env(),
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=full_prompt.encode("utf-8")), timeout=180
    )
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()[:300]
        out = stdout.decode("utf-8", errors="replace").strip()[:200]
        import logging
        logging.getLogger("okoone.claude").error(
            "Claude CLI failed (rc=%d) stderr=%s stdout=%s prompt_len=%d",
            proc.returncode, err, out, len(full_prompt),
        )
        raise RuntimeError(f"Claude CLI error (rc={proc.returncode}): {err or out or 'no output'}")
    return stdout.decode("utf-8", errors="replace").strip()


class ClaudeAdvisor:
    """Orchestrates self-improving loop via Claude CLI."""

    async def _build_context(self, db) -> str:
        """Build a context summary from current CRM state for Claude."""
        parts: list[str] = []

        # Current scoring weights
        async with db.execute(
            "SELECT criteria_json FROM scoring_weights WHERE is_active = 1 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                parts.append(f"## Poids de scoring actuels\n```json\n{row[0]}\n```")

        # Recent reviews with score breakdown
        async with db.execute("""
            SELECT p.full_name, p.headline, p.current_company, p.relevance_score,
                   p.score_breakdown, h.reviewer_verdict, h.feedback_text
            FROM human_reviews h
            JOIN prospects p ON p.id = h.prospect_id
            ORDER BY h.reviewed_at DESC LIMIT 20
        """) as cursor:
            reviews = await cursor.fetchall()
            if reviews:
                lines = ["## Dernieres reviews (20)"]
                for r in reviews:
                    verdict_emoji = {"approve": "+", "reject": "-", "flag": "?"}
                    lines.append(
                        f"[{verdict_emoji.get(r[5], '?')}] {r[0]} | {r[1]} @ {r[2]} | "
                        f"score={r[3]} | breakdown={r[4]} | feedback={r[6] or '-'}"
                    )
                parts.append("\n".join(lines))

        # Search queries with yield
        async with db.execute("""
            SELECT sq.id, sq.keywords, sq.location, sq.total_results,
                   COALESCE(
                       (SELECT COUNT(*) FROM human_reviews hr
                        JOIN prospects p ON p.id = hr.prospect_id
                        WHERE p.source_search_id = sq.id AND hr.reviewer_verdict = 'approve'),
                       0
                   ) as approved,
                   COALESCE(
                       (SELECT COUNT(*) FROM prospects p WHERE p.source_search_id = sq.id), 0
                   ) as total_prospects
            FROM search_queries sq WHERE sq.is_active = 1
        """) as cursor:
            queries = await cursor.fetchall()
            if queries:
                lines = ["## Queries de recherche actives"]
                for q in queries:
                    yield_pct = (q[4] / q[5] * 100) if q[5] > 0 else 0
                    lines.append(
                        f"- [{q[0]}] \"{q[1]}\" @ {q[2] or 'any'} | "
                        f"{q[5]} prospects, {q[4]} approuves ({yield_pct:.0f}% yield)"
                    )
                parts.append("\n".join(lines))

        # Eval metrics
        async with db.execute(
            "SELECT * FROM eval_snapshots ORDER BY created_at DESC LIMIT 1"
        ) as cursor:
            snap = await cursor.fetchone()
            if snap:
                parts.append(
                    f"## Dernieres metriques\n"
                    f"Precision={snap[2]:.2f} Rappel={snap[3]:.2f} F1={snap[4]:.2f} "
                    f"Top-K={snap[5]:.2f} Accord={snap[6]:.0f}%"
                )

        # Stats summary
        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'discovered' THEN 1 ELSE 0 END) as discovered,
                SUM(CASE WHEN status = 'qualified' THEN 1 ELSE 0 END) as qualified,
                SUM(CASE WHEN status = 'contacted' THEN 1 ELSE 0 END) as contacted,
                AVG(relevance_score) as avg_score
            FROM prospects
        """) as cursor:
            stats = await cursor.fetchone()
            if stats:
                parts.append(
                    f"## Pipeline\n"
                    f"Total: {stats[0]} | Discovered: {stats[1]} | Qualified: {stats[2]} | "
                    f"Contacted: {stats[3]} | Score moyen: {stats[4]:.1f}"
                )

        return "\n\n".join(parts) if parts else "Aucune donnee encore."

    async def chat(self, db, user_message: str, conversation_history: list[dict] | None = None) -> str:
        """Send a message to Claude with full CRM context. Returns Claude's response."""
        context = await self._build_context(db)

        messages: list[dict] = []

        # Include conversation history if provided
        if conversation_history:
            messages.extend(conversation_history)

        # Add current message with context
        messages.append({
            "role": "user",
            "content": f"## Contexte CRM (auto-genere)\n{context}\n\n---\n\n## Ma question\n{user_message}",
        })

        prompt = f"## Contexte CRM (auto-genere)\n{context}\n\n---\n\n## Ma question\n{user_message}"
        return await _call_claude(prompt)

    async def analyze_and_propose(self, db) -> dict:
        """Run a full analysis cycle: ask Claude to review current state and propose improvements.

        Returns {
            "analysis": str,
            "proposed_weights": dict | None,
            "proposed_queries": list[dict] | None,
            "proposed_traits": list[str] | None,
        }
        """
        context = await self._build_context(db)

        prompt = (
            f"## Contexte CRM\n{context}\n\n---\n\n"
            "Fais une analyse complete de l'etat actuel du pipeline de prospection:\n"
            "1. Identifie les patterns dans les reviews (qu'est-ce qui distingue les approuves des rejetes ?)\n"
            "2. Propose des ajustements de poids de scoring (JSON format)\n"
            "3. Propose 2-3 nouvelles queries de recherche LinkedIn qui pourraient avoir un bon yield\n"
            "4. Identifie des traits ou signaux qu'on ne capture pas encore\n"
            "5. Donne un verdict global: le scoring s'ameliore ou se degrade ?\n\n"
            "Reponds avec les sections suivantes en markdown:\n"
            "### Analyse\n### Poids proposes\n```json\n{...}\n```\n"
            "### Queries proposees\n```json\n[{...}]\n```\n"
            "### Nouveaux traits\n### Verdict"
        )

        text = await _call_claude(prompt)

        # Try to extract structured data from the response
        result: dict = {"analysis": text, "proposed_weights": None, "proposed_queries": None, "proposed_traits": None}

        # Extract JSON blocks
        import re

        json_blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        for block in json_blocks:
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict) and any(k in parsed for k in ("title_match", "company_fit", "seniority")):
                    result["proposed_weights"] = parsed
                elif isinstance(parsed, list):
                    result["proposed_queries"] = parsed
            except json.JSONDecodeError:
                continue

        return result

    async def suggest_email_approach(self, db, prospect_id: int) -> str:
        """Ask Claude to suggest a personalized outreach approach for a prospect."""
        async with db.execute(
            "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
        ) as cursor:
            prospect = await cursor.fetchone()
            if not prospect:
                return "Prospect introuvable."

        columns = [desc[0] for desc in cursor.description]
        p = dict(zip(columns, prospect))

        prompt = (
            f"Voici un prospect pour Okoone (agence digitale, equipes on-demand, dev web/mobile/cloud):\n\n"
            f"**{p.get('full_name')}** — {p.get('headline')}\n"
            f"Entreprise: {p.get('current_company')} | Localisation: {p.get('location')}\n"
            f"Experience: {p.get('experience_json', '[]')}\n"
            f"Score: {p.get('relevance_score')}/100\n\n"
            "Propose:\n"
            "1. Un angle d'approche personnalise (pourquoi Okoone les interesse)\n"
            "2. Un sujet d'email accrocheur\n"
            "3. Un message court (3-4 phrases max)"
        )

        return await _call_claude(prompt)
