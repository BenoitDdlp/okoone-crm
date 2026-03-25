"""Unit tests for prospect deduplication (upsert) logic in ProspectRepository.

Each test uses an in-memory SQLite database via the ``test_db`` fixture.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from app.repositories.prospect_repo import ProspectRepository


@pytest.mark.asyncio
async def test_upsert_new_prospect(test_db, sample_prospect: dict) -> None:
    """Upserting a username that does not exist should create a new record."""
    repo = ProspectRepository(test_db)

    username = sample_prospect["linkedin_username"]
    data = {k: v for k, v in sample_prospect.items() if k != "linkedin_username"}

    prospect_id, is_new = await repo.upsert_by_username(username, data)

    assert is_new is True
    assert isinstance(prospect_id, int)
    assert prospect_id > 0

    # Verify it is in the database
    row = await repo.find_by_username(username)
    assert row is not None
    assert row["full_name"] == "Jane Doe"
    assert row["location"] == "Singapore"


@pytest.mark.asyncio
async def test_upsert_existing_prospect(test_db, sample_prospect: dict) -> None:
    """Upserting an existing username should update the record and return is_new=False."""
    repo = ProspectRepository(test_db)

    username = sample_prospect["linkedin_username"]
    data = {k: v for k, v in sample_prospect.items() if k != "linkedin_username"}

    # First insert
    first_id, first_new = await repo.upsert_by_username(username, data)
    assert first_new is True

    # Second upsert with updated data
    updated_data = {
        "full_name": "Jane Doe-Updated",
        "headline": "CTO & Co-Founder at NewVenture",
        "location": "Ho Chi Minh City, Vietnam",
    }
    second_id, second_new = await repo.upsert_by_username(username, updated_data)

    assert second_new is False
    assert second_id == first_id

    # Verify the update took effect
    row = await repo.find_by_username(username)
    assert row is not None
    assert row["full_name"] == "Jane Doe-Updated"
    assert row["headline"] == "CTO & Co-Founder at NewVenture"
    assert row["location"] == "Ho Chi Minh City, Vietnam"


@pytest.mark.asyncio
async def test_upsert_preserves_manual_edits(test_db) -> None:
    """Manual notes and flags should NOT be overwritten by a scrape upsert.

    The upsert only updates fields explicitly passed in the data dict,
    so notes/flags set by a human should survive a re-scrape.
    """
    repo = ProspectRepository(test_db)

    username = "preserved-user"

    # Initial insert from scrape
    initial_data = {
        "full_name": "Preserved User",
        "headline": "VP Engineering",
        "location": "Bangkok, Thailand",
    }
    prospect_id, _ = await repo.upsert_by_username(username, initial_data)

    # Simulate a human adding notes and flags
    await repo.update(prospect_id, {
        "notes": "Met at conference -- very interested",
        "flags_json": json.dumps(["priority", "warm-lead"]),
    })

    # Verify manual edits are saved
    row_before = await repo.find_by_id(prospect_id)
    assert row_before is not None
    assert row_before["notes"] == "Met at conference -- very interested"
    assert "priority" in row_before["flags_json"]

    # Now re-scrape: upsert with updated scrape data (no notes/flags)
    rescrape_data = {
        "full_name": "Preserved User",
        "headline": "SVP Engineering",  # title changed
        "location": "Bangkok, Thailand",
    }
    reinserted_id, is_new = await repo.upsert_by_username(username, rescrape_data)
    assert is_new is False
    assert reinserted_id == prospect_id

    # Verify the scrape data was updated
    row_after = await repo.find_by_id(prospect_id)
    assert row_after is not None
    assert row_after["headline"] == "SVP Engineering"

    # Verify manual edits are still intact
    assert row_after["notes"] == "Met at conference -- very interested"
    assert "priority" in row_after["flags_json"]
