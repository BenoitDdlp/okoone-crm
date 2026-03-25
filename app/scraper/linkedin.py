"""LinkedIn scraper using Patchright (async Playwright fork for stealth).

Manages a persistent Chromium profile so LinkedIn sessions survive restarts.
All network access is gated through a ``RateLimiter`` to stay within safe
daily quotas and inject human-like timing jitter.
"""

from __future__ import annotations

import asyncio
import logging
import random
from urllib.parse import quote

from app.scraper.parser import parse_profile_page, parse_search_results
from app.scraper.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# User-agent rotation pool
# ------------------------------------------------------------------ #

_USER_AGENTS: list[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
]


def get_random_user_agent() -> str:
    """Return a randomly selected modern browser user-agent string."""
    return random.choice(_USER_AGENTS)


# ------------------------------------------------------------------ #
# Scraper
# ------------------------------------------------------------------ #


class LinkedInScraper:
    """Async LinkedIn scraper backed by Patchright (stealth Playwright fork).

    Usage::

        rl = RateLimiter()
        scraper = LinkedInScraper(profile_dir="./browser_profile", rate_limiter=rl)
        await scraper.start()
        try:
            results = await scraper.search_people("CTO SaaS")
        finally:
            await scraper.stop()
    """

    def __init__(self, profile_dir: str, rate_limiter: RateLimiter) -> None:
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._profile_dir = profile_dir
        self._rate_limiter = rate_limiter

    # -------------------------------------------------------------- #
    # Lifecycle
    # -------------------------------------------------------------- #

    async def start(self) -> None:
        """Launch headless Chromium with a persistent profile for session reuse."""
        from patchright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self._profile_dir,
            headless=True,
            viewport={
                "width": 1280 + random.randint(-50, 50),
                "height": 800 + random.randint(-50, 50),
            },
            user_agent=get_random_user_agent(),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )
        self._page = await self._browser.new_page()
        logger.info("LinkedIn scraper browser started (profile: %s)", self._profile_dir)

    async def stop(self) -> None:
        """Close the browser and Playwright server."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                logger.warning("Error closing browser context", exc_info=True)
            self._browser = None
            self._page = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                logger.warning("Error stopping Playwright", exc_info=True)
            self._pw = None
        logger.info("LinkedIn scraper browser stopped")

    # -------------------------------------------------------------- #
    # Session helpers
    # -------------------------------------------------------------- #

    async def is_session_valid(self) -> bool:
        """Check whether the LinkedIn session is still active.

        Navigates to the feed page and inspects the final URL.  If LinkedIn
        redirects to a login or checkpoint page the session is expired.
        """
        if not self._page:
            return False
        try:
            await self._page.goto(
                "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
            )
            current_url = self._page.url
            return "/login" not in current_url and "/checkpoint" not in current_url
        except Exception:
            logger.warning("Session validity check failed", exc_info=True)
            return False

    async def get_cookies(self) -> list[dict]:
        """Return all cookies from the current browser context."""
        if not self._browser:
            return []
        return await self._browser.cookies()

    async def set_cookies(self, cookies: list[dict]) -> None:
        """Load cookies into the current browser context."""
        if self._browser and cookies:
            await self._browser.add_cookies(cookies)

    # -------------------------------------------------------------- #
    # Search
    # -------------------------------------------------------------- #

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
        page: int = 1,
    ) -> list[dict[str, str]]:
        """Search LinkedIn people by keyword.

        Returns a list of dicts with keys:
            full_name, headline, location, linkedin_url, profile_username,
            connection_degree
        """
        if not self._page:
            raise RuntimeError("Scraper not started; call start() first")

        await self._rate_limiter.acquire("search")

        query = (
            f"https://www.linkedin.com/search/results/people/"
            f"?keywords={quote(keywords)}&origin=GLOBAL_SEARCH_HEADER"
        )
        if location:
            query += f"&geoUrn=&location={quote(location)}"
        if page > 1:
            query += f"&page={page}"

        logger.info("Searching LinkedIn people: %s (page %d)", keywords, page)
        await self._page.goto(query, wait_until="domcontentloaded")
        await self._human_scroll()

        # Wait briefly for JS-rendered content to settle
        await asyncio.sleep(random.uniform(1.0, 2.0))

        content = await self._page.content()
        results = parse_search_results(content)
        logger.info("Found %d results for '%s'", len(results), keywords)
        return results

    # -------------------------------------------------------------- #
    # Profile
    # -------------------------------------------------------------- #

    async def get_person_profile(self, username: str) -> dict:
        """Fetch and parse a full LinkedIn profile by username.

        Returns a structured dict including experience, education, skills, etc.
        """
        if not self._page:
            raise RuntimeError("Scraper not started; call start() first")

        await self._rate_limiter.acquire("profile")

        url = f"https://www.linkedin.com/in/{quote(username, safe='')}/"
        logger.info("Fetching LinkedIn profile: %s", username)
        await self._page.goto(url, wait_until="domcontentloaded")
        await self._human_scroll()

        # Give the page a moment to finish lazy-loading sections
        await asyncio.sleep(random.uniform(1.5, 3.0))

        content = await self._page.content()
        profile = parse_profile_page(content, username)
        logger.info(
            "Parsed profile for %s (%s)",
            profile.get("full_name", "?"),
            username,
        )
        return profile

    # -------------------------------------------------------------- #
    # Anti-detection helpers
    # -------------------------------------------------------------- #

    async def _human_scroll(self) -> None:
        """Simulate human-like scrolling to trigger lazy-loaded content."""
        if not self._page:
            return
        scroll_count = random.randint(2, 4)
        for _ in range(scroll_count):
            await self._page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.5, 1.5))
        # Occasionally scroll back up a tiny bit
        if random.random() < 0.3:
            await self._page.mouse.wheel(0, -random.randint(50, 150))
            await asyncio.sleep(random.uniform(0.3, 0.7))
