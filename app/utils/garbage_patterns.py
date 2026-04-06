"""Single source of truth for garbage-data patterns.

Used by the scraper, scheduler pre-filter, and scoring service so that
all three layers reject the same LinkedIn UI artefacts.
"""

from __future__ import annotations

import re

# ------------------------------------------------------------------ #
# Compiled patterns (for regex-level checks)
# ------------------------------------------------------------------ #

GARBAGE_NAME_RE = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"join linkedin",
        r"not you\?",
        r"remove photo",
        r"^provides services",
        r"^status is (offline|online|reachable)",
        r"view .+['\u2018\u2019]s\s+profile",
        r"^sign in",
        r"^ACoAA",
        r"can introduce you to \d+",
        r"\d+\s*(st|nd|rd|th)\s*degree connection",
        r"linkedin member",
        r"\d+\s*subscribers",
        r"\d+\s*reactions",
        r"3rd\+",
        r"2nd\+",
    ]
]

GARBAGE_LOCATION_RE = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"seek to live",
        r"currently behind",
        r"\d+k?\s*subscribers",
        r"amazing.*journey",
        r"\d+\s*reactions",
        r"not you",
        r"join linkedin",
        r"remove photo",
        r"behind live",
        r"degree connection",
    ]
]

GARBAGE_HEADLINE_RE = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^provides services",
        r"sign in to view",
        r"^\u2022\s*\d",  # bullet + number (connection degree)
        r"degree connection",
        r"\d+\s*subscribers",
        r"\d+\s*reactions",
        r"amazing.*journey",
        r"behind live",
        r"join linkedin",
        r"not you\?",
        r"remove photo",
        r"can introduce you to",
    ]
]

# ------------------------------------------------------------------ #
# Simple word lists (for `in` checks — backward compat)
# ------------------------------------------------------------------ #

GARBAGE_NAME_WORDS: list[str] = [
    "provides services", "status is", "subscribers", "reactions",
    "join linkedin", "not you?", "remove photo", "sign in",
    "degree connection", "3rd+", "2nd+", "1st+",
    "can introduce you to", "linkedin member", "view",
]

GARBAGE_LOCATION_WORDS: list[str] = [
    "seek", "behind live", "subscribers", "amazing", "journey",
    "reactions", "currently behind", "degree connection",
    "seek to live", "not you", "join linkedin", "remove photo",
]

GARBAGE_HEADLINE_WORDS: list[str] = [
    "degree connection", "3rd+", "2nd", "1st",
    "provides services", "subscribers", "reactions",
    "amazing journey", "behind live",
    "join linkedin", "not you?", "remove photo", "sign in",
    "can introduce you to",
]


# ------------------------------------------------------------------ #
# Helper functions
# ------------------------------------------------------------------ #

def is_garbage_name(name: str) -> bool:
    """Return True if *name* looks like a LinkedIn UI artefact."""
    if not name or len(name.split()) < 2:
        return True
    return any(p.search(name) for p in GARBAGE_NAME_RE)


def is_garbage_location(loc: str) -> bool:
    """Return True if *loc* is a LinkedIn UI string, not a real place."""
    if not loc:
        return False  # empty handled elsewhere
    return any(p.search(loc) for p in GARBAGE_LOCATION_RE)


def is_garbage_headline(headline: str) -> bool:
    """Return True if *headline* is a LinkedIn UI artefact."""
    if not headline:
        return False
    return any(p.search(headline) for p in GARBAGE_HEADLINE_RE)
