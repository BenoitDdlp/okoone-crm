"""Rate limiter tuned for LinkedIn anti-detection.

Safe limits (from 2025/2026 research):
- New account ramp-up: 10 profiles/day week 1, +10/week until 50
- Mature account: 50 profile views/day, 15 searches/day
- Delays: 30-90s between profile views, 45-120s between searches
- Spread actions over 8+ hours, not bursts
- Random jitter on everything
"""

import asyncio
import time
import random
from datetime import datetime, timedelta


class DailyLimitReached(Exception):
    pass


class RateLimiter:
    """Conservative rate limiter designed for a NEW LinkedIn account.

    Starts very slow and ramps up over weeks to avoid detection.
    """

    def __init__(
        self,
        account_created_date: str = "2026-03-25",
        max_daily_profiles: int = 50,
        max_daily_searches: int = 15,
    ) -> None:
        self._account_born = datetime.strptime(account_created_date, "%Y-%m-%d")
        self._max_profiles = max_daily_profiles
        self._max_searches = max_daily_searches

        self._limits: dict[str, dict] = {
            "search": {"min_delay": 45, "max_delay": 120},
            "profile": {"min_delay": 30, "max_delay": 90},
        }
        self._counts: dict[str, int] = {"search": 0, "profile": 0}
        self._last_action: dict[str, float] = {"search": 0.0, "profile": 0.0}
        self._reset_date: str = ""

    def _account_age_weeks(self) -> int:
        return max(0, (datetime.now() - self._account_born).days // 7)

    def _daily_limit(self, action_type: str) -> int:
        """Ramp up limits based on account age.

        Week 0-1: 10 profiles, 5 searches (baby account)
        Week 2:   20 profiles, 8 searches
        Week 3:   30 profiles, 10 searches
        Week 4+:  full limits (50 profiles, 15 searches)
        """
        weeks = self._account_age_weeks()

        if action_type == "profile":
            base = self._max_profiles
            if weeks <= 1:
                return min(10, base)
            elif weeks == 2:
                return min(20, base)
            elif weeks == 3:
                return min(30, base)
            return base

        if action_type == "search":
            base = self._max_searches
            if weeks <= 1:
                return min(5, base)
            elif weeks == 2:
                return min(8, base)
            elif weeks == 3:
                return min(10, base)
            return base

        return 10

    async def acquire(self, action_type: str) -> None:
        """Wait until the next action is allowed. Raises DailyLimitReached if exhausted."""
        self._maybe_reset_daily()

        limit = self._daily_limit(action_type)
        if self._counts[action_type] >= limit:
            raise DailyLimitReached(
                f"{action_type} daily limit ({limit}) reached "
                f"(account age: {self._account_age_weeks()} weeks)"
            )

        config = self._limits[action_type]
        elapsed = time.monotonic() - self._last_action[action_type]

        # Randomized delay with extra jitter for new accounts
        base_delay = random.uniform(config["min_delay"], config["max_delay"])
        age_multiplier = max(1.0, 2.0 - self._account_age_weeks() * 0.25)
        delay = base_delay * age_multiplier

        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)

        self._counts[action_type] += 1
        self._last_action[action_type] = time.monotonic()

    def get_remaining(self, action_type: str) -> int:
        self._maybe_reset_daily()
        return self._daily_limit(action_type) - self._counts[action_type]

    def get_stats(self) -> dict:
        self._maybe_reset_daily()
        weeks = self._account_age_weeks()
        return {
            "account_age_weeks": weeks,
            "search": {
                "used": self._counts["search"],
                "limit": self._daily_limit("search"),
                "remaining": self.get_remaining("search"),
            },
            "profile": {
                "used": self._counts["profile"],
                "limit": self._daily_limit("profile"),
                "remaining": self.get_remaining("profile"),
            },
        }

    def _maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._reset_date != today:
            self._counts = {k: 0 for k in self._limits}
            self._reset_date = today
