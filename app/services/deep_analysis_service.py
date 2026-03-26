"""Deep prospect analysis powered by Claude CLI.

Goes beyond keyword matching: asks Claude to reason about a prospect's
fit against the active program, acquaintances, and Okoone's positioning.
Produces a structured JSON analysis stored in the prospects table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

from app.services.claude_advisor import _call_claude

logger = logging.getLogger("okoone.deep_analysis")

ANALYSIS_SYSTEM = (
    "Tu es un expert en qualification de prospects B2B pour Okoone, "
    "une agence digitale premium basee en Asie du Sud-Est (equipes on-demand: "
    "dev web/mobile, cloud, data, AI). Tu analyses des profils LinkedIn "
    "et produis un jugement structure et honnete sur leur pertinence.\n\n"
    "Tu reponds UNIQUEMENT en JSON valide, sans commentaire autour. "
    "Aucune cloture markdown (pas de ```json). Juste le JSON brut."
)


class DeepAnalysisService:
    """Uses Claude CLI to produce a rich, structured analysis for each prospect."""

    async def analyze_prospect(
        self,
        prospect: dict,
        program: str,
        acquaintances: list[dict],
    ) -> dict:
        """Deeply analyze a prospect against the program.

        Returns a dict with:
            score: 0-100,
            verdict: "strong_match"|"good_fit"|"maybe"|"pass",
            summary: "2-3 sentence summary",
            pros: ["list of strengths"],
            cons: ["list of weaknesses"],
            outreach_angle: "suggested approach",
            fit_reasoning: "why this person fits (or not) Okoone's needs",
        """
        profile_text = self._build_profile_text(prospect)
        acq_text = self._build_acquaintances_text(acquaintances)

        prompt = (
            f"## Programme de prospection actif\n{program}\n\n"
            f"## Profils de reference (acquaintances)\n{acq_text}\n\n"
            f"## Prospect a analyser\n{profile_text}\n\n"
            "## Instructions\n"
            "Analyse ce prospect en profondeur par rapport au programme "
            "et aux profils de reference. Produis un JSON avec EXACTEMENT "
            "cette structure (rien d'autre):\n\n"
            "{\n"
            '  "score": <int 0-100>,\n'
            '  "verdict": "<strong_match|good_fit|maybe|pass>",\n'
            '  "summary": "<2-3 phrases de synthese>",\n'
            '  "pros": ["<force 1>", "<force 2>", ...],\n'
            '  "cons": ["<faiblesse 1>", "<faiblesse 2>", ...],\n'
            '  "outreach_angle": "<angle d\'approche personnalise>",\n'
            '  "fit_reasoning": "<raisonnement detaille sur l\'adequation>"\n'
            "}\n\n"
            "Criteres d'evaluation:\n"
            "- Le titre/role indique-t-il un pouvoir de decision sur l'externalisation tech ?\n"
            "- L'entreprise est-elle dans un secteur cible (fintech, healthtech, SaaS, etc.) ?\n"
            "- La taille d'equipe et la trajectoire suggerent-elles un besoin d'externalisation ?\n"
            "- La localisation est-elle strategique (Singapore, SEA prioritaire) ?\n"
            "- Y a-t-il des signaux positifs (recrute des devs, petite equipe tech, background agence) ?\n"
            "- Y a-t-il des signaux negatifs (trop grosse equipe tech interne, concurrent, profil non-tech) ?\n"
            "- Le parcours est-il similaire aux acquaintances positives ?\n"
            "- Quel angle d'approche serait le plus pertinent pour cette personne ?\n\n"
            "Sois honnete et tranchant. Un 'pass' est mieux qu'un faux positif."
        )

        raw = await _call_claude(prompt, system=ANALYSIS_SYSTEM)
        return self._parse_response(raw, prospect)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_profile_text(self, p: dict) -> str:
        """Build a readable text summary of the prospect for the prompt."""
        lines: list[str] = []
        lines.append(f"**Nom**: {p.get('full_name', 'Inconnu')}")
        lines.append(f"**Titre**: {p.get('current_title', p.get('headline', 'N/A'))}")
        lines.append(f"**Headline**: {p.get('headline', 'N/A')}")
        lines.append(f"**Entreprise**: {p.get('current_company', 'N/A')}")
        lines.append(f"**Localisation**: {p.get('location', 'N/A')}")

        if p.get("about_text"):
            about = p["about_text"][:800]
            lines.append(f"**A propos**: {about}")

        # Experience
        experience = self._parse_json_field(p.get("experience_json"))
        if experience:
            lines.append("**Experience**:")
            for exp in experience[:6]:
                if isinstance(exp, dict):
                    title = exp.get("title", "?")
                    company = exp.get("company", "?")
                    duration = exp.get("duration", "")
                    desc = (exp.get("description") or "")[:200]
                    line = f"  - {title} @ {company}"
                    if duration:
                        line += f" ({duration})"
                    if desc:
                        line += f" — {desc}"
                    lines.append(line)

        # Education
        education = self._parse_json_field(p.get("education_json"))
        if education:
            lines.append("**Education**:")
            for edu in education[:3]:
                if isinstance(edu, dict):
                    school = edu.get("school", "?")
                    degree = edu.get("degree", "")
                    lines.append(f"  - {school} {degree}")

        # Skills
        skills = self._parse_json_field(p.get("skills_json"))
        if skills:
            skill_names = []
            for s in skills[:15]:
                if isinstance(s, dict):
                    skill_names.append(s.get("name", str(s)))
                elif isinstance(s, str):
                    skill_names.append(s)
            if skill_names:
                lines.append(f"**Competences**: {', '.join(skill_names)}")

        lines.append(f"**Score algorithmique**: {p.get('relevance_score', 0)}/100")
        if p.get("score_summary"):
            lines.append(f"**Resume du score**: {p['score_summary']}")

        return "\n".join(lines)

    def _build_acquaintances_text(self, acquaintances: list[dict]) -> str:
        """Build a text block for the acquaintances context."""
        if not acquaintances:
            return "Aucune acquaintance de reference."

        lines: list[str] = []
        for a in acquaintances:
            polarity = "POSITIF" if a.get("is_positive_example") else "NEGATIF"
            lines.append(
                f"- [{polarity}] {a.get('full_name', '?')} — "
                f"{a.get('headline', '')} @ {a.get('company', '')} "
                f"({a.get('relationship', '')})"
            )
            if a.get("notes"):
                lines.append(f"  Notes: {a['notes']}")
        return "\n".join(lines)

    def _parse_json_field(self, raw: str | list | None) -> list:
        """Safely parse a JSON field that may be a string, list, or None."""
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _parse_response(self, raw: str, prospect: dict) -> dict:
        """Extract structured JSON from Claude's response, with fallbacks."""
        # Try direct JSON parse first
        try:
            result = json.loads(raw.strip())
            if isinstance(result, dict) and "score" in result:
                return self._validate_result(result)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try to extract JSON from markdown code blocks
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(1).strip())
                if isinstance(result, dict) and "score" in result:
                    return self._validate_result(result)
            except (json.JSONDecodeError, TypeError):
                pass

        # Try to find any JSON object in the response
        brace_match = re.search(r"\{[\s\S]*\}", raw)
        if brace_match:
            try:
                result = json.loads(brace_match.group(0))
                if isinstance(result, dict):
                    return self._validate_result(result)
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: build a minimal result from raw text
        logger.warning(
            "Could not parse Claude response for %s, using fallback. Raw length=%d",
            prospect.get("full_name", "?"),
            len(raw),
        )
        return {
            "score": 0,
            "verdict": "maybe",
            "summary": raw[:300] if raw else "Analyse non structuree.",
            "pros": [],
            "cons": [],
            "outreach_angle": "",
            "fit_reasoning": raw[:500] if raw else "",
            "parse_error": True,
        }

    def _validate_result(self, result: dict) -> dict:
        """Ensure all expected fields are present with correct types."""
        validated: dict = {}
        validated["score"] = max(0, min(100, int(result.get("score", 0))))

        verdict = result.get("verdict", "maybe")
        if verdict not in ("strong_match", "good_fit", "maybe", "pass"):
            verdict = "maybe"
        validated["verdict"] = verdict

        validated["summary"] = str(result.get("summary", ""))[:500]

        pros = result.get("pros", [])
        validated["pros"] = [str(p) for p in pros] if isinstance(pros, list) else []

        cons = result.get("cons", [])
        validated["cons"] = [str(c) for c in cons] if isinstance(cons, list) else []

        validated["outreach_angle"] = str(result.get("outreach_angle", ""))[:500]
        validated["fit_reasoning"] = str(result.get("fit_reasoning", ""))[:1000]
        validated["analyzed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return validated

    # ------------------------------------------------------------------
    # Web research enrichment
    # ------------------------------------------------------------------

    _WEB_RESEARCH_SYSTEM = (
        "You are a web research assistant. Search the web for information about "
        "people and companies. Return ONLY valid JSON — no markdown fences, no "
        "commentary. If a field cannot be found, use an empty string or empty array."
    )

    _WEB_RESEARCH_FIELDS: dict[str, str | list] = {
        "company_website": "",
        "company_size": "",
        "company_industry": "",
        "company_funding": "",
        "company_description": "",
        "recent_news": [],
        "technologies": [],
        "social_presence": "",
    }

    async def web_research_prospect(self, prospect: dict) -> dict:
        """Use Claude CLI (with web search) to find additional info about a prospect.

        Returns {
            company_website: str,
            company_size: str,
            company_industry: str,
            company_funding: str,
            company_description: str,
            recent_news: [str],
            technologies: [str],
            social_presence: str,
        }
        """
        name = prospect.get("full_name", "Unknown")
        company = prospect.get("current_company", "Unknown")
        title = prospect.get("current_title") or prospect.get("headline", "N/A")
        location = prospect.get("location", "N/A")

        prompt = (
            f"Search the web for information about {name}, who works at {company} "
            f"as {title} in {location}.\n\n"
            "Find:\n"
            "1. Company website URL\n"
            "2. Company size (employees count)\n"
            "3. Company industry/sector\n"
            "4. Company funding (if startup — recent rounds, total raised)\n"
            "5. Short company description (1-2 sentences)\n"
            "6. Recent news about the person or company (last 12 months)\n"
            "7. Technologies/tools the company uses\n"
            "8. Person's social media presence beyond LinkedIn\n\n"
            "Return ONLY a JSON object with these fields:\n"
            "{company_website, company_size, company_industry, company_funding, "
            "company_description, recent_news: [], technologies: [], social_presence}\n\n"
            "If info not found, use empty string or empty array."
        )

        try:
            raw = await asyncio.wait_for(
                _call_claude(prompt, system=self._WEB_RESEARCH_SYSTEM),
                timeout=60,
            )
        except asyncio.TimeoutError:
            logger.warning("Web research timed out for %s @ %s", name, company)
            return self._empty_web_research()
        except Exception:
            logger.error("Web research failed for %s @ %s:", name, company, exc_info=True)
            return self._empty_web_research()

        return self._parse_web_research(raw, prospect)

    def _empty_web_research(self) -> dict:
        """Return a blank web-research result dict."""
        return {k: ([] if isinstance(v, list) else "") for k, v in self._WEB_RESEARCH_FIELDS.items()}

    def _parse_web_research(self, raw: str, prospect: dict) -> dict:
        """Extract web-research JSON from Claude's response, with fallbacks."""
        # Direct parse
        try:
            result = json.loads(raw.strip())
            if isinstance(result, dict):
                return self._validate_web_research(result)
        except (json.JSONDecodeError, TypeError):
            pass

        # Extract from markdown code block
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(1).strip())
                if isinstance(result, dict):
                    return self._validate_web_research(result)
            except (json.JSONDecodeError, TypeError):
                pass

        # Find any JSON object in the text
        brace_match = re.search(r"\{[\s\S]*\}", raw)
        if brace_match:
            try:
                result = json.loads(brace_match.group(0))
                if isinstance(result, dict):
                    return self._validate_web_research(result)
            except (json.JSONDecodeError, TypeError):
                pass

        logger.warning(
            "Could not parse web research response for %s, using empty. Raw length=%d",
            prospect.get("full_name", "?"), len(raw),
        )
        return self._empty_web_research()

    def _validate_web_research(self, result: dict) -> dict:
        """Normalise web-research dict to expected types."""
        validated: dict = {}
        for key, default in self._WEB_RESEARCH_FIELDS.items():
            val = result.get(key, default)
            if isinstance(default, list):
                if isinstance(val, list):
                    validated[key] = [str(v) for v in val]
                elif isinstance(val, str) and val:
                    validated[key] = [val]
                else:
                    validated[key] = []
            else:
                validated[key] = str(val) if val else ""
        validated["researched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return validated
