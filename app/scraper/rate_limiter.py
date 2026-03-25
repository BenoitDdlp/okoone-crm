import asyncio
import time
import random


class DailyLimitReached(Exception):
    """Raised when daily scraping quota for an action type is exhausted."""

    pass


class RateLimiter:
    """Token bucket rate limiter with daily limits and jitter.

    Ensures LinkedIn scraping stays within conservative daily thresholds
    and introduces randomised delays between actions to mimic human cadence.
    """

    def __init__(
        self,
        daily_search_limit: int = 30,
        daily_profile_limit: int = 80,
    ) -> None:
        self._limits: dict[str, dict[str, int]] = {
            "search": {
                "daily_max": daily_search_limit,
                "min_delay": 15,
                "max_delay": 30,
            },
            "profile": {
                "daily_max": daily_profile_limit,
                "min_delay": 20,
                "max_delay": 45,
            },
        }
        self._counts: dict[str, int] = {"search": 0, "profile": 0}
        self._last_action: dict[str, float] = {"search": 0.0, "profile": 0.0}
        self._reset_date: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, action_type: str) -> None:
        """Wait until the next action of *action_type* is allowed.

        Raises ``DailyLimitReached`` when the daily quota is exhausted.
        """
        self._maybe_reset_daily()

        config = self._limits[action_type]

        if self._counts[action_type] >= config["daily_max"]:
            raise DailyLimitReached(
                f"{action_type} daily limit ({config['daily_max']}) reached"
            )

        # Enforce randomised minimum gap between consecutive actions.
        elapsed = time.monotonic() - self._last_action[action_type]
        delay = random.uniform(config["min_delay"], config["max_delay"])
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)

        self._counts[action_type] += 1
        self._last_action[action_type] = time.monotonic()

    def get_remaining(self, action_type: str) -> int:
        """Return how many actions of *action_type* are left today."""
        self._maybe_reset_daily()
        return self._limits[action_type]["daily_max"] - self._counts[action_type]

    def get_stats(self) -> dict[str, dict[str, int]]:
        """Return usage statistics for every tracked action type."""
        self._maybe_reset_daily()
        return {
            k: {"used": self._counts[k], "remaining": self.get_remaining(k)}
            for k in self._limits
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._reset_date != today:
            self._counts = {k: 0 for k in self._limits}
            self._reset_date = today
