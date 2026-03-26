"""Fix bad prospect data in the CRM database.

Corrects names that show "Status is offline", "View X's profile" garbage,
or "Provides services" blobs. Extracts real names from:
  1) The headline field (which often contains "Real NameView Real Name's profile")
  2) The location field (which sometimes holds the real name)
  3) The linkedin_username (e.g. "sylvain-emery" -> "Sylvain Emery")

Also cleans up headline and location fields that contain profile-view text.

Run directly:  python3 scripts/fix_prospect_data.py [--dry-run]
"""

from __future__ import annotations

import re
import sqlite3
import sys
from urllib.parse import unquote


DB_PATH = "db/okoone_crm.sqlite"

# Patterns indicating bad name data
BAD_NAME_PATTERNS = [
    re.compile(r"^Status is (offline|online|reachable)$", re.IGNORECASE),
    re.compile(r"View .+'s profile", re.IGNORECASE),
    re.compile(r"^Provides services", re.IGNORECASE),
    re.compile(r"^ACoAA"),  # LinkedIn internal IDs leaking as names
]


def is_bad_name(name: str | None) -> bool:
    """Return True if the name is garbage / needs fixing."""
    if not name or len(name.strip()) < 2:
        return True
    for pat in BAD_NAME_PATTERNS:
        if pat.search(name):
            return True
    return False


def extract_name_from_headline(headline: str | None) -> str:
    """Extract a real name from a headline like 'Sylvain EmeryView Sylvain Emery\u2019s profile'.

    The pattern is: <RealName>View <RealName>'s profile
    We extract the name that appears after 'View' and before "'s profile".
    """
    if not headline:
        return ""
    # Pattern: ...View <Name>'s profile (with curly or straight apostrophe)
    m = re.search(r"View\s+(.+?)[\u2018\u2019']\s*s\s+profile\s*$", headline)
    if m:
        return m.group(1).strip()
    # Simpler: headline IS the name (already clean from JS extractor)
    # but only if it doesn't contain View/profile garbage
    if "View " not in headline and "'s profile" not in headline and "\u2019s profile" not in headline:
        return ""
    return ""


def extract_name_from_location(location: str | None) -> str:
    """Some records have the real name in the location field (misplaced by the fallback parser).

    Only return it if it looks like a person name (no commas, no 'View', no 'Status').
    """
    if not location:
        return ""
    loc = location.strip()
    # Skip if it looks like an actual location (has comma) or garbage
    if "," in loc or "View " in loc or "Status " in loc or "profile" in loc.lower():
        return ""
    # A name should be 2-5 capitalized words
    words = loc.split()
    if 2 <= len(words) <= 6 and all(w[0].isupper() or w[0] == '"' for w in words if w):
        return loc
    return ""


def _split_camel_or_run(word: str) -> list[str]:
    """Try to split a concatenated username into name parts.

    Examples:
        sylvainemery -> ['sylvainemery']  (can't reliably split)
        ryannorton1 -> ['ryannorton']  (strip trailing digits)
        gregoryorleans -> ['gregoryorleans']
    """
    # Strip trailing digits
    word = re.sub(r"\d+$", "", word)
    if not word:
        return []
    # Try camelCase split (rare but possible)
    parts = re.findall(r"[A-Z][a-z]+|[a-z]+", word)
    if len(parts) >= 2 and all(len(p) >= 2 for p in parts):
        return parts
    return [word]


def name_from_username(username: str) -> str:
    """Convert a LinkedIn username to a plausible name.

    Examples:
        sylvain-emery -> Sylvain Emery
        oscar-alonso-plaza-2442a9168 -> Oscar Alonso Plaza
        yvan-r%C3%A9geard-b788901 -> Yvan Regeard
        ryannorton1 -> Ryannorton
        dan-corrigan-director -> Dan Corrigan (drops non-name words)
    """
    # URL-decode first
    decoded = unquote(username)
    # Remove trailing hash suffixes (8+ hex/digits after a dash)
    decoded = re.sub(r"-[0-9a-f]{6,}$", "", decoded)
    # Split on hyphens
    parts = decoded.split("-")
    # Filter out pure numbers (LinkedIn ID suffixes)
    name_parts = [p for p in parts if p and not p.isdigit()]
    # Drop common non-name suffixes that appear in some usernames
    non_name_words = {"director", "cto", "ceo", "cfo", "coo", "vp", "manager",
                      "engineer", "dev", "techpm", "tech", "pm"}
    name_parts = [p for p in name_parts if p.lower() not in non_name_words]
    if not name_parts:
        return ""

    # If there's only one part (no hyphens), try camelCase/run splitting
    if len(name_parts) == 1:
        sub = _split_camel_or_run(name_parts[0])
        name_parts = sub if sub else name_parts

    # Title-case each part
    return " ".join(p.capitalize() for p in name_parts)


def clean_headline(headline: str | None) -> str:
    """Remove 'View X's profile' suffix from headline."""
    if not headline:
        return ""
    # Remove the 'View X's profile' suffix
    cleaned = re.sub(r"View\s+.+?[\u2018\u2019']\s*s\s+profile\s*$", "", headline).strip()
    # If that was the entire headline, return empty
    return cleaned


def clean_location(location: str | None, real_name: str) -> str:
    """Clean location field. If it contains the person's name, clear it."""
    if not location:
        return ""
    loc = location.strip()
    # If location IS the real name (common bug), clear it
    if real_name and loc.lower() == real_name.lower():
        return ""
    # Remove View...profile garbage
    if "View " in loc and "profile" in loc:
        return ""
    # Remove Status is offline
    if re.match(r"^Status is (offline|online|reachable)$", loc, re.IGNORECASE):
        return ""
    return loc


def fix_prospects(db_path: str, dry_run: bool = False) -> None:
    """Main cleanup logic."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    rows = db.execute(
        "SELECT id, full_name, headline, location, linkedin_username FROM prospects ORDER BY id"
    ).fetchall()

    fixes = []
    for row in rows:
        pid = row["id"]
        name = row["full_name"] or ""
        headline = row["headline"] or ""
        location = row["location"] or ""
        username = row["linkedin_username"] or ""

        # Skip if username is a LinkedIn internal ID (ACoAA...) - can't recover
        if username.startswith("ACoAA"):
            print(f"  [SKIP] id={pid} username={username} (LinkedIn internal ID, cannot recover)")
            continue

        updates: dict[str, str] = {}

        # --- Fix name ---
        if is_bad_name(name):
            new_name = ""
            # Strategy 1: extract from headline ("RealNameView RealName's profile")
            new_name = extract_name_from_headline(headline)
            # Strategy 2: extract from location (sometimes the real name ended up there)
            if not new_name:
                new_name = extract_name_from_location(location)
            # Strategy 3: derive from username
            if not new_name:
                new_name = name_from_username(username)
            if new_name and new_name != name:
                updates["full_name"] = new_name
        else:
            # Name is OK but might have "View X's profile" suffix
            cleaned = re.sub(r"View\s+.+?[\u2018\u2019']\s*s\s+profile\s*$", "", name).strip()
            if cleaned != name and cleaned:
                updates["full_name"] = cleaned
                new_name = cleaned
            else:
                new_name = name

        # Use the final resolved name for location cleaning
        final_name = updates.get("full_name", name)

        # --- Fix headline ---
        cleaned_hl = clean_headline(headline)
        if cleaned_hl != headline:
            updates["headline"] = cleaned_hl

        # --- Fix location ---
        cleaned_loc = clean_location(location, final_name)
        if cleaned_loc != location:
            updates["location"] = cleaned_loc

        if updates:
            fixes.append((pid, username, updates))

    # Apply fixes
    print(f"\nTotal prospects: {len(rows)}")
    print(f"Prospects to fix: {len(fixes)}")
    print()

    for pid, username, updates in fixes:
        parts = [f"{k}: '{v}'" for k, v in updates.items()]
        print(f"  [{pid}] {username}: {', '.join(parts)}")
        if not dry_run:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [pid]
            db.execute(
                f"UPDATE prospects SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
                values,
            )

    if not dry_run and fixes:
        db.commit()
        print(f"\n  Applied {len(fixes)} fixes.")
    elif dry_run and fixes:
        print(f"\n  DRY RUN: {len(fixes)} fixes would be applied.")

    db.close()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN MODE ===\n")
    fix_prospects(DB_PATH, dry_run=dry_run)
