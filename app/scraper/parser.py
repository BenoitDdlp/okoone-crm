"""Parse LinkedIn HTML pages into structured data.

Uses regex and stdlib ``html`` for unescaping.  Every extraction helper
returns a sensible default (empty string / empty list) rather than raising,
so callers always get at least a partial result.
"""

from __future__ import annotations

import html as html_mod
import re
from typing import Optional


# ------------------------------------------------------------------ #
# Utility helpers
# ------------------------------------------------------------------ #

def _unescape(text: str) -> str:
    """HTML-unescape and collapse whitespace."""
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _first_match(pattern: str, text: str, group: int = 1) -> str:
    """Return the first regex match or empty string."""
    m = re.search(pattern, text, re.DOTALL)
    return _unescape(m.group(group)) if m else ""


def _all_matches(pattern: str, text: str, group: int = 1) -> list[str]:
    return [_unescape(m.group(group)) for m in re.finditer(pattern, text, re.DOTALL)]


# ------------------------------------------------------------------ #
# Search results parser
# ------------------------------------------------------------------ #

def parse_search_results(page_html: str) -> list[dict[str, str]]:
    """Parse a LinkedIn people-search results page.

    Returns a list of dicts with keys:
        full_name, headline, location, linkedin_url, profile_username,
        connection_degree
    """
    results: list[dict[str, str]] = []

    # LinkedIn wraps each result card in a <li> with class containing
    # "reusable-search__result-container".  We split the page into those
    # blocks and parse each independently.
    card_blocks = re.split(
        r'<li[^>]*class="[^"]*reusable-search__result-container[^"]*"',
        page_html,
    )

    for block in card_blocks[1:]:  # skip everything before the first card
        # --- profile URL & username ----------------------------------
        url_match = re.search(
            r'href="(https://www\.linkedin\.com/in/([^/?\"]+))[^"]*"',
            block,
        )
        if not url_match:
            # Fallback: some markup uses relative paths
            url_match = re.search(
                r'href="(/in/([^/?\"]+))[^"]*"',
                block,
            )
        if not url_match:
            continue  # not a valid people result

        linkedin_url = url_match.group(1)
        if linkedin_url.startswith("/"):
            linkedin_url = f"https://www.linkedin.com{linkedin_url}"
        profile_username = url_match.group(2)

        # --- full name -----------------------------------------------
        # Typically inside <span aria-hidden="true"> right after the link
        full_name = _first_match(
            r'<span[^>]*aria-hidden="true"[^>]*>([^<]+)</span>',
            block,
        )
        if not full_name:
            # Alternate: the <a> tag title attribute
            full_name = _first_match(r'title="([^"]+)"', block)

        # --- headline ------------------------------------------------
        headline = _first_match(
            r'<div[^>]*class="[^"]*entity-result__primary-subtitle[^"]*"[^>]*>'
            r'\s*<span[^>]*>([^<]+)',
            block,
        )
        if not headline:
            headline = _first_match(
                r'entity-result__primary-subtitle[^>]*>([^<]+)', block
            )

        # --- location ------------------------------------------------
        location = _first_match(
            r'<div[^>]*class="[^"]*entity-result__secondary-subtitle[^"]*"[^>]*>'
            r'\s*<span[^>]*>([^<]+)',
            block,
        )
        if not location:
            location = _first_match(
                r'entity-result__secondary-subtitle[^>]*>([^<]+)', block
            )

        # --- connection degree ----------------------------------------
        degree = _first_match(
            r'<span[^>]*class="[^"]*entity-result__badge-text[^"]*"[^>]*>'
            r'\s*([^<]+)',
            block,
        )
        if not degree:
            degree_match = re.search(r'(\d)(?:st|nd|rd)', block)
            degree = degree_match.group(0) if degree_match else ""

        results.append(
            {
                "full_name": full_name,
                "headline": headline,
                "location": location,
                "linkedin_url": linkedin_url,
                "profile_username": profile_username,
                "connection_degree": degree,
            }
        )

    return results


# ------------------------------------------------------------------ #
# Profile page parser
# ------------------------------------------------------------------ #

def parse_profile_page(page_html: str, username: str) -> dict:
    """Parse a LinkedIn profile page.

    Returns a dict with keys:
        full_name, headline, location, about, current_company,
        current_title, experience, education, skills, profile_photo_url
    """
    profile: dict = {
        "username": username,
        "full_name": "",
        "headline": "",
        "location": "",
        "about": "",
        "current_company": "",
        "current_title": "",
        "experience": [],
        "education": [],
        "skills": [],
        "profile_photo_url": "",
    }

    try:
        profile["full_name"] = _extract_profile_name(page_html)
    except Exception:
        pass

    try:
        profile["headline"] = _extract_profile_headline(page_html)
    except Exception:
        pass

    try:
        profile["location"] = _extract_profile_location(page_html)
    except Exception:
        pass

    try:
        profile["about"] = _extract_about(page_html)
    except Exception:
        pass

    try:
        profile["profile_photo_url"] = _extract_photo_url(page_html)
    except Exception:
        pass

    try:
        experience = _extract_experience(page_html)
        profile["experience"] = experience
        if experience:
            profile["current_company"] = experience[0].get("company", "")
            profile["current_title"] = experience[0].get("title", "")
    except Exception:
        pass

    try:
        profile["education"] = _extract_education(page_html)
    except Exception:
        pass

    try:
        profile["skills"] = _extract_skills(page_html)
    except Exception:
        pass

    return profile


# ------------------------------------------------------------------ #
# Individual field extractors
# ------------------------------------------------------------------ #

def _extract_profile_name(html: str) -> str:
    # The profile name lives in <h1> with class containing "text-heading-xlarge"
    name = _first_match(
        r'<h1[^>]*class="[^"]*text-heading-xlarge[^"]*"[^>]*>([^<]+)</h1>',
        html,
    )
    if not name:
        # Fallback: og:title meta tag
        name = _first_match(
            r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"', html
        )
        # og:title often appends " | LinkedIn"
        name = re.sub(r"\s*\|\s*LinkedIn.*$", "", name)
    return name


def _extract_profile_headline(html: str) -> str:
    return _first_match(
        r'<div[^>]*class="[^"]*text-body-medium[^"]*break-words[^"]*"[^>]*>\s*([^<]+)',
        html,
    )


def _extract_profile_location(html: str) -> str:
    return _first_match(
        r'<span[^>]*class="[^"]*text-body-small[^"]*inline[^"]*t-black--light[^"]*"'
        r'[^>]*>\s*([^<]+)',
        html,
    )


def _extract_about(html: str) -> str:
    # About section is typically in a <section> with id "about"
    about_section = _first_match(
        r'<section[^>]*id="about"[^>]*>(.*?)</section>', html
    )
    if about_section:
        # Get the main text block inside
        text = _first_match(
            r'<span[^>]*aria-hidden="true"[^>]*>(.*?)</span>', about_section
        )
        if text:
            return re.sub(r"<[^>]+>", " ", text).strip()
    # Fallback: look for the about inline content
    return _first_match(
        r'inline-show-more-text[^>]*>.*?<span[^>]*aria-hidden="true"[^>]*>'
        r'(.*?)</span>',
        html,
    )


def _extract_photo_url(html: str) -> str:
    # Profile photo is in an <img> with class containing "pv-top-card-profile-picture"
    url = _first_match(
        r'<img[^>]*class="[^"]*pv-top-card-profile-picture[^"]*"[^>]*src="([^"]+)"',
        html,
    )
    if not url:
        # Fallback: og:image meta tag (lower-res thumbnail)
        url = _first_match(
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html
        )
    return url


def _extract_experience(html: str) -> list[dict[str, str]]:
    """Extract experience entries from the profile page.

    Returns list of {company, title, duration, description}.
    """
    experiences: list[dict[str, str]] = []

    # Experience section
    exp_section = _first_match(
        r'<section[^>]*id="experience"[^>]*>(.*?)</section>', html
    )
    if not exp_section:
        # Fallback: look for section containing "Experience" heading
        exp_section = _first_match(
            r'<section[^>]*>.*?Experience.*?</h2>(.*?)</section>', html
        )

    if not exp_section:
        return experiences

    # Each experience item is inside an <li> within the experience section
    items = re.split(r"<li[^>]*class=\"[^\"]*pvs-list__paged-list-item", exp_section)

    for item in items[1:]:  # skip preamble
        title = _first_match(
            r'<span[^>]*aria-hidden="true"[^>]*>([^<]+)</span>', item
        )
        company = ""
        duration = ""
        description = ""

        # Company name is often the second aria-hidden span
        spans = _all_matches(
            r'<span[^>]*aria-hidden="true"[^>]*>([^<]+)</span>', item
        )
        if len(spans) >= 2:
            company = spans[1]
        if len(spans) >= 3:
            duration = spans[2]

        # Description may be in a longer text block
        desc_match = _first_match(
            r'inline-show-more-text[^>]*>.*?<span[^>]*aria-hidden="true"[^>]*>'
            r'(.*?)</span>',
            item,
        )
        if desc_match:
            description = re.sub(r"<[^>]+>", " ", desc_match).strip()

        if title or company:
            experiences.append(
                {
                    "company": company,
                    "title": title,
                    "duration": duration,
                    "description": description,
                }
            )

    return experiences


def _extract_education(html: str) -> list[dict[str, str]]:
    """Extract education entries.

    Returns list of {school, degree, field, years}.
    """
    education: list[dict[str, str]] = []

    edu_section = _first_match(
        r'<section[^>]*id="education"[^>]*>(.*?)</section>', html
    )
    if not edu_section:
        edu_section = _first_match(
            r'<section[^>]*>.*?Education.*?</h2>(.*?)</section>', html
        )

    if not edu_section:
        return education

    items = re.split(r"<li[^>]*class=\"[^\"]*pvs-list__paged-list-item", edu_section)

    for item in items[1:]:
        spans = _all_matches(
            r'<span[^>]*aria-hidden="true"[^>]*>([^<]+)</span>', item
        )
        school = spans[0] if len(spans) >= 1 else ""
        degree = spans[1] if len(spans) >= 2 else ""
        field = ""
        years = ""

        # Degree and field are sometimes combined: "Bachelor's degree, Computer Science"
        if "," in degree:
            parts = degree.split(",", 1)
            degree = parts[0].strip()
            field = parts[1].strip()

        # Years are typically in a <span> containing a date range pattern
        years_match = re.search(r"(\d{4})\s*[-\u2013]\s*(\d{4})", item)
        if years_match:
            years = f"{years_match.group(1)} - {years_match.group(2)}"

        if school:
            education.append(
                {
                    "school": school,
                    "degree": degree,
                    "field": field,
                    "years": years,
                }
            )

    return education


def _extract_skills(html: str) -> list[str]:
    """Extract skills list from the profile page."""
    skills_section = _first_match(
        r'<section[^>]*id="skills"[^>]*>(.*?)</section>', html
    )
    if not skills_section:
        skills_section = _first_match(
            r'<section[^>]*>.*?Skills.*?</h2>(.*?)</section>', html
        )

    if not skills_section:
        return []

    # Each skill appears inside aria-hidden spans within the skills section
    raw_skills = _all_matches(
        r'<span[^>]*aria-hidden="true"[^>]*>([^<]+)</span>', skills_section
    )

    # Deduplicate while preserving order, and drop items that look like
    # metadata ("endorsements", numbers, etc.)
    seen: set[str] = set()
    skills: list[str] = []
    noise_pattern = re.compile(
        r"^\d+$|endorse|see\s+all|show\s+(more|less)|skill", re.IGNORECASE
    )
    for s in raw_skills:
        normalised = s.strip()
        if normalised and normalised.lower() not in seen and not noise_pattern.search(normalised):
            seen.add(normalised.lower())
            skills.append(normalised)

    return skills
