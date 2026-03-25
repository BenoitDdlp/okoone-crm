"""Unit tests for the RateLimiter.

Tests cover delay enforcement, daily limits, daily reset, and stats reporting.
Time-dependent behaviour is controlled via monkeypatching.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from app.scraper.rate_limiter import DailyLimitReached, RateLimiter


@pytest.mark.asyncio
async def test_acquire_respects_delay() -> None:
    """The second ``acquire`` should wait at least the configured min_delay.

    We use a limiter with a very small window (0.05-0.1 s) so the test stays fast.
    """
    rl = RateLimiter(daily_search_limit=100, daily_profile_limit=100)

    # Override delay config to make the test fast
    rl._limits["search"]["min_delay"] = 0.05
    rl._limits["search"]["max_delay"] = 0.10

    t0 = time.monotonic()
    await rl.acquire("search")
    t1 = time.monotonic()
    await rl.acquire("search")
    t2 = time.monotonic()

    first_duration = t1 - t0
    second_duration = t2 - t1

    # The first acquire should be near-instant (only jitter if nothing came before).
    # The second acquire must wait at least min_delay minus the first call's elapsed.
    # We just assert the second call took *some* measurable time.
    assert second_duration >= 0.04, (
        f"Second acquire should have waited; took only {second_duration:.4f}s"
    )


@pytest.mark.asyncio
async def test_daily_limit_reached() -> None:
    """Exhausting the daily quota should raise ``DailyLimitReached``."""
    rl = RateLimiter(daily_search_limit=2, daily_profile_limit=100)

    # Zero out delays for speed
    rl._limits["search"]["min_delay"] = 0
    rl._limits["search"]["max_delay"] = 0

    await rl.acquire("search")
    await rl.acquire("search")

    with pytest.raises(DailyLimitReached):
        await rl.acquire("search")


@pytest.mark.asyncio
async def test_daily_reset() -> None:
    """Changing the date should reset the daily counters."""
    rl = RateLimiter(daily_search_limit=1, daily_profile_limit=100)
    rl._limits["search"]["min_delay"] = 0
    rl._limits["search"]["max_delay"] = 0

    # Use up the daily limit
    await rl.acquire("search")

    with pytest.raises(DailyLimitReached):
        await rl.acquire("search")

    # Simulate a date change by resetting the internal reset_date
    rl._reset_date = "1970-01-01"

    # Now acquire should succeed because the counter is reset
    await rl.acquire("search")
    assert rl._counts["search"] == 1


def test_get_remaining() -> None:
    """``get_remaining`` should reflect used vs limit."""
    rl = RateLimiter(daily_search_limit=30, daily_profile_limit=80)
    assert rl.get_remaining("search") == 30
    assert rl.get_remaining("profile") == 80

    # Manually consume some
    rl._counts["search"] = 10
    assert rl.get_remaining("search") == 20


def test_get_stats() -> None:
    """``get_stats`` should return a dict with used/remaining per action type."""
    rl = RateLimiter(daily_search_limit=30, daily_profile_limit=80)
    rl._counts["search"] = 5
    rl._counts["profile"] = 20

    stats = rl.get_stats()

    assert "search" in stats
    assert "profile" in stats
    assert stats["search"]["used"] == 5
    assert stats["search"]["remaining"] == 25
    assert stats["profile"]["used"] == 20
    assert stats["profile"]["remaining"] == 60
