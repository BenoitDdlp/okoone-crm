from __future__ import annotations

import json
import re
from difflib import SequenceMatcher


class ScoringService:
    """Configurable weighted scoring engine for prospect qualification."""

    # ------------------------------------------------------------------
    # Seniority keyword map — value is the normalised seniority score (0-1)
    # ------------------------------------------------------------------

    SENIORITY_MAP: dict[float, list[str]] = {
        1.0: ["ceo", "founder", "co-founder", "president", "owner"],
        0.9: ["cto", "cio", "cdo", "cpo", "chief"],
        0.8: ["vp", "vice president", "evp", "svp"],
        0.7: ["director", "head of"],
        0.6: ["senior manager", "principal"],
        0.5: ["manager", "lead", "team lead"],
        0.3: ["senior", "sr."],
        0.1: ["junior", "jr.", "intern", "trainee"],
    }

    TARGET_INDUSTRIES: list[str] = [
        "fintech",
        "healthtech",
        "edtech",
        "saas",
        "ecommerce",
        "e-commerce",
        "digital",
        "technology",
        "software",
        "startup",
        "ai",
        "blockchain",
    ]

    TARGET_TITLES: list[str] = [
        "cto",
        "vp engineering",
        "head of engineering",
        "head of digital",
        "head of product",
        "it director",
        "chief digital",
        "chief technology",
        "founder",
        "ceo",
        "technical director",
    ]

    # Location tiers: region keywords → score
    LOCATION_TIERS: list[tuple[list[str], float]] = [
        (["singapore"], 1.0),
        (
            [
                "vietnam",
                "thailand",
                "indonesia",
                "malaysia",
                "philippines",
                "cambodia",
                "myanmar",
                "laos",
                "brunei",
                "ho chi minh",
                "hanoi",
                "bangkok",
                "jakarta",
                "kuala lumpur",
                "manila",
                "phnom penh",
            ],
            0.7,
        ),
        (
            [
                "japan",
                "korea",
                "south korea",
                "taiwan",
                "hong kong",
                "china",
                "india",
                "australia",
                "new zealand",
                "tokyo",
                "seoul",
                "sydney",
                "melbourne",
                "mumbai",
                "delhi",
                "bangalore",
                "bengaluru",
            ],
            0.5,
        ),
        (
            [
                "united states",
                "usa",
                "uk",
                "united kingdom",
                "germany",
                "france",
                "canada",
                "netherlands",
                "switzerland",
                "europe",
            ],
            0.3,
        ),
    ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def score_prospect(
        self, prospect: dict, weights: dict
    ) -> tuple[float, dict]:
        """Score a prospect. Returns (total_score, breakdown_dict)."""
        breakdown: dict[str, float] = {}
        breakdown["title_match"] = self._score_title(prospect)
        breakdown["company_fit"] = self._score_company(prospect)
        breakdown["seniority"] = self._score_seniority(prospect)
        breakdown["industry"] = self._score_industry(prospect)
        breakdown["location"] = self._score_location(prospect)
        breakdown["completeness"] = self._score_completeness(prospect)
        breakdown["activity"] = self._score_activity(prospect)

        # ---- Hard gate: no company AND no location → score 0 ----
        company = (prospect.get("current_company") or "").strip()
        location = (prospect.get("location") or "").strip()
        if not company and not location:
            return 0.0, breakdown

        # ---- Hard gate: junior/intern titles → cap at 15 ----
        title_text = (prospect.get("current_title") or prospect.get("headline") or "").lower()
        junior_patterns = ["junior", "jr.", "intern", "trainee", "entry level", "entry-level", "associate", "assistant"]
        if any(pat in title_text for pat in junior_patterns) and breakdown["seniority"] <= 0.1:
            weight_sum = sum(weights.get(k, 0) for k in breakdown)
            if weight_sum == 0:
                return 0.0, breakdown
            total = sum(breakdown[k] * weights.get(k, 0) for k in breakdown) / weight_sum
            return min(round(total * 100, 1), 15.0), breakdown

        weight_sum = sum(weights.get(k, 0) for k in breakdown)
        if weight_sum == 0:
            return 0.0, breakdown

        total = sum(breakdown[k] * weights.get(k, 0) for k in breakdown) / weight_sum
        return round(total * 100, 1), breakdown

    # ------------------------------------------------------------------
    # Score summary
    # ------------------------------------------------------------------

    def generate_score_summary(self, breakdown: dict, weights: dict, prospect: dict | None = None) -> str:
        """Generate a short pro/con summary with 5-word max human insights."""
        pros: list[str] = []
        cons: list[str] = []
        mid: list[tuple[str, float]] = []

        for key, value in breakdown.items():
            insight = self._insight_for(key, value, prospect or {})
            if not insight:
                continue
            if value >= 0.7:
                pros.append(f"+ {insight}")
            elif value <= 0.3:
                cons.append(f"- {insight}")
            else:
                mid.append((key, value))

        lines = pros + cons

        # If fewer than 3 bullets, fill from mid-range criteria
        if len(lines) < 3:
            for key, value in mid:
                if len(lines) >= 3:
                    break
                insight = self._insight_for(key, value, prospect or {})
                if not insight:
                    continue
                if value >= 0.5:
                    lines.append(f"+ {insight}")
                else:
                    lines.append(f"- {insight}")

        return "\n".join(lines)

    @staticmethod
    def _insight_for(key: str, value: float, prospect: dict) -> str:
        """Return a concise 5-word-max human insight for a criterion."""
        title = (prospect.get("current_title") or prospect.get("headline") or "").strip()
        company = (prospect.get("current_company") or "").strip()
        location = (prospect.get("location") or "").strip()

        if key == "title_match":
            if value >= 0.7:
                short = title[:30] if title else "Strong"
                return f"{short} decision maker"
            if value >= 0.4:
                return "Moderate title relevance"
            return "Non-target job title"

        if key == "company_fit":
            if value >= 0.7:
                short = company[:25] if company else "Tech/startup"
                return f"{short} tech company"
            if value >= 0.4:
                return "Average company fit"
            return "Unknown small company"

        if key == "seniority":
            if value >= 0.7:
                return "CTO/VP level leader"
            if value >= 0.4:
                return "Mid-level seniority"
            return "Too junior for outreach"

        if key == "industry":
            if value >= 0.7:
                return "Strong tech industry match"
            if value >= 0.4:
                return "Partial industry relevance"
            return "Low industry relevance"

        if key == "location":
            if value >= 0.7:
                loc_short = location[:20] if location else "SEA"
                return f"{loc_short} prime location"
            if value >= 0.4:
                return "APAC region location"
            return "Remote or distant location"

        if key == "completeness":
            if value >= 0.7:
                return "Rich complete profile"
            if value >= 0.4:
                return "Partially complete profile"
            return "No email or contact info"

        if key == "activity":
            if value >= 0.7:
                return "Active engaged profile"
            if value >= 0.4:
                return "Moderate profile activity"
            return "Inactive or sparse profile"

        return ""

    # ------------------------------------------------------------------
    # Individual scoring functions — each returns 0.0..1.0
    # ------------------------------------------------------------------

    def _score_title(self, p: dict) -> float:
        """0-1 fuzzy match against TARGET_TITLES."""
        title = (p.get("current_title") or p.get("headline") or "").lower().strip()
        if not title:
            return 0.0

        best = 0.0
        for target in self.TARGET_TITLES:
            # Exact substring match is best
            if target in title:
                ratio = 1.0
            else:
                ratio = SequenceMatcher(None, title, target).ratio()
            if ratio > best:
                best = ratio

        # Also check headline if title didn't match well
        headline = (p.get("headline") or "").lower().strip()
        if headline and headline != title:
            for target in self.TARGET_TITLES:
                if target in headline:
                    ratio = 0.9  # slight penalty for headline vs title
                else:
                    ratio = SequenceMatcher(None, headline, target).ratio() * 0.9
                if ratio > best:
                    best = ratio

        return min(best, 1.0)

    def _score_company(self, p: dict) -> float:
        """Company size signals, agency vs enterprise distinction."""
        company = (p.get("current_company") or "").lower().strip()
        headline = (p.get("headline") or "").lower()
        experience_raw = p.get("experience_json") or "[]"

        try:
            experience = json.loads(experience_raw) if isinstance(experience_raw, str) else experience_raw
        except (json.JSONDecodeError, TypeError):
            experience = []

        if not company:
            return 0.0

        score = 0.3  # non-empty company baseline, boosted by keyword matches

        # Enterprise / large company signals
        enterprise_keywords = [
            "inc", "corp", "corporation", "group", "holdings",
            "international", "global", "limited", "ltd",
        ]
        for kw in enterprise_keywords:
            if kw in company:
                score = max(score, 0.7)
                break

        # Tech company signals (higher value)
        tech_keywords = [
            "tech", "software", "digital", "labs", "ai",
            "data", "cloud", "platform", "solutions",
        ]
        for kw in tech_keywords:
            if kw in company:
                score = max(score, 0.8)
                break

        # Startup / scale-up signals (highest for Okoone's target)
        startup_keywords = ["startup", "start-up", "ventures", "stealth"]
        for kw in startup_keywords:
            if kw in company or kw in headline:
                score = max(score, 0.9)
                break

        # Multiple previous companies suggests breadth
        if isinstance(experience, list) and len(experience) >= 3:
            score = min(score + 0.1, 1.0)

        # Penalise agencies (less likely to be buyers)
        agency_keywords = [
            "agency", "freelance", "consultant", "consulting firm",
            "recruitment", "staffing", "headhunting",
        ]
        for kw in agency_keywords:
            if kw in company or kw in headline:
                score = max(score - 0.3, 0.0)
                break

        return round(score, 2)

    def _score_seniority(self, p: dict) -> float:
        """From SENIORITY_MAP based on title/headline."""
        title = (p.get("current_title") or "").lower()
        headline = (p.get("headline") or "").lower()
        text = f"{title} {headline}"

        best = 0.0
        for seniority_score, keywords in self.SENIORITY_MAP.items():
            for kw in keywords:
                if kw in text:
                    best = max(best, seniority_score)
        return best

    def _score_industry(self, p: dict) -> float:
        """Keyword match in headline/experience for target industries."""
        headline = (p.get("headline") or "").lower()
        about = (p.get("about_text") or "").lower()
        company = (p.get("current_company") or "").lower()

        experience_raw = p.get("experience_json") or "[]"
        try:
            experience = json.loads(experience_raw) if isinstance(experience_raw, str) else experience_raw
        except (json.JSONDecodeError, TypeError):
            experience = []

        # Build a combined text from experience
        exp_text = ""
        if isinstance(experience, list):
            for exp in experience:
                if isinstance(exp, dict):
                    exp_text += " " + (exp.get("company", "") + " " + exp.get("title", "") + " " + exp.get("description", "")).lower()

        combined = f"{headline} {about} {company} {exp_text}"

        matches = sum(1 for ind in self.TARGET_INDUSTRIES if ind in combined)
        if matches == 0:
            return 0.0
        if matches == 1:
            return 0.4
        if matches == 2:
            return 0.7
        return min(0.7 + matches * 0.1, 1.0)

    def _score_location(self, p: dict) -> float:
        """Singapore=1.0, SEA=0.7, APAC=0.5, West=0.3, unknown=0.2."""
        location = (p.get("location") or "").lower().strip()
        if not location:
            return 0.0  # no location = no score

        for keywords, tier_score in self.LOCATION_TIERS:
            for kw in keywords:
                if kw in location:
                    return tier_score

        # No match in any tier
        return 0.1

    def _score_completeness(self, p: dict) -> float:
        """Profile completeness: has email, about, experience, education, skills."""
        total_fields = 6
        present = 0

        if p.get("contact_email"):
            present += 1
        if p.get("about_text"):
            present += 1

        # Check JSON arrays are non-empty
        for field in ("experience_json", "education_json", "skills_json"):
            raw = p.get(field) or "[]"
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, list) and len(parsed) > 0:
                    present += 1
            except (json.JSONDecodeError, TypeError):
                pass

        if p.get("profile_photo_url"):
            present += 1

        return round(present / total_fields, 2)

    def _score_activity(self, p: dict) -> float:
        """Placeholder for activity scoring (needs post data).

        For now, use heuristics based on available data:
        - Has about text → some engagement signal
        - Has skills → profile maintenance signal
        - Recent screened_at → recently active
        """
        score = 0.0  # no free points — must be earned

        if p.get("about_text") and len(p.get("about_text", "")) > 100:
            score += 0.2

        skills_raw = p.get("skills_json") or "[]"
        try:
            skills = json.loads(skills_raw) if isinstance(skills_raw, str) else skills_raw
            if isinstance(skills, list) and len(skills) >= 5:
                score += 0.2
        except (json.JSONDecodeError, TypeError):
            pass

        if p.get("screened_at"):
            score += 0.1

        # Cap at 1.0
        return min(round(score, 2), 1.0)
