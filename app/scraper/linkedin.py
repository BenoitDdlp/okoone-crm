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

import re as _re

from app.scraper.parser import parse_search_results
from app.scraper.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Post-extraction sanitisation
# ------------------------------------------------------------------ #

_GARBAGE_NAME_PATTERNS = [
    _re.compile(r"^Status is (offline|online|reachable)$", _re.IGNORECASE),
    _re.compile(r"View .+[\u2018\u2019']s\s+profile", _re.IGNORECASE),
    _re.compile(r"^Provides services", _re.IGNORECASE),
    _re.compile(r"^ACoA"),
]

_GARBAGE_NAME_PREFIXES = [
    "Join LinkedIn", "LinkedIn Member", "Sign in",
    "Provides services", "ACoAA", "ACoA",
]

_VIEW_PROFILE_SUFFIX = _re.compile(
    r"View\s+.+[\u2018\u2019']s\s+profile\s*$", _re.IGNORECASE
)


def _is_garbage_name(name: str) -> bool:
    """Return True if *name* is clearly not a real person name."""
    if not name or len(name) < 2:
        return True
    for prefix in _GARBAGE_NAME_PREFIXES:
        if name.startswith(prefix):
            return True
    # Names longer than 80 chars are almost certainly service descriptions
    if len(name) > 80:
        return True
    for pat in _GARBAGE_NAME_PATTERNS:
        if pat.search(name):
            return True
    return False


def _is_acoa_username(username: str) -> bool:
    """Return True if *username* is an internal LinkedIn ACoAA identifier."""
    return bool(username) and username.startswith("ACoA")


def _sanitize_search_results(results: list[dict[str, str]]) -> list[dict[str, str]]:
    """Clean up known garbage patterns from search results.

    This is a safety net that runs after both the primary and fallback
    extractors, so even if the DOM parsing lets something through, we
    catch it here before it reaches the database.
    """
    clean: list[dict[str, str]] = []
    for r in results:
        # Filter out ACoAA internal IDs masquerading as usernames —
        # these profiles cannot be deep-screened anyway.
        username = r.get("profile_username", "")
        if _is_acoa_username(username):
            logger.debug(
                "Dropping result with ACoAA username: %s (%s)",
                r.get("full_name"),
                username,
            )
            continue

        name = r.get("full_name", "").strip()
        headline = r.get("headline", "").strip()
        location = r.get("location", "").strip()

        # Step 1: Strip "View X's profile" suffix from name (handles
        # "Georges S.View Georges S.'s profile" -> "Georges S.")
        name = _VIEW_PROFILE_SUFFIX.sub("", name).strip()

        # Step 2: Check if the (possibly cleaned) name is still garbage
        name_is_bad = _is_garbage_name(name)

        if name_is_bad:
            # Try to recover name from headline ("RealNameView RealName's profile")
            m = _re.search(
                r"View\s+(.+?)[\u2018\u2019']\s*s\s+profile\s*$", headline
            )
            if m:
                name = m.group(1).strip()
            elif headline and len(headline) >= 2:
                # Headline itself might be a clean name (from earlier extraction)
                if not _is_garbage_name(headline):
                    # Don't use headline as name if it looks like a job title
                    pass  # safer to drop than to misassign
                # Cannot recover a meaningful name; skip this result entirely
                if not name or len(name) < 2:
                    logger.debug(
                        "Dropping result with unrecoverable name: %s (%s)",
                        r.get("full_name"),
                        r.get("profile_username"),
                    )
                    continue
            else:
                logger.debug(
                    "Dropping result with unrecoverable name: %s (%s)",
                    r.get("full_name"),
                    r.get("profile_username"),
                )
                continue

        # Clean headline: remove "View X's profile" suffix
        headline = _VIEW_PROFILE_SUFFIX.sub("", headline).strip()

        # Clean location: remove status indicators and profile view text
        if _re.match(r"^Status is ", location, _re.IGNORECASE):
            location = ""
        if "View " in location and "profile" in location:
            location = ""

        r["full_name"] = name
        r["headline"] = headline
        r["location"] = location
        clean.append(r)

    dropped = len(results) - len(clean)
    if dropped > 0:
        logger.info("Sanitisation dropped %d results with bad data", dropped)

    return clean


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
        import os
        from patchright.async_api import async_playwright

        # Remove stale lock files that prevent profile reuse
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = os.path.join(self._profile_dir, lock_file)
            if os.path.exists(lock_path):
                logger.info("Removing stale lock file: %s", lock_path)
                os.unlink(lock_path)

        logger.info("Starting browser with profile: %s", self._profile_dir)
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
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )
        self._page = await self._browser.new_page()
        logger.info("Browser started OK. Pages: %d", len(self._browser.pages))

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
            logger.warning("is_session_valid: no page object")
            return False
        try:
            await self._page.goto(
                "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
            )
            await asyncio.sleep(2)
            current_url = self._page.url
            valid = "/login" not in current_url and "/checkpoint" not in current_url
            logger.info("is_session_valid: url=%s valid=%s", current_url, valid)
            return valid
        except Exception:
            logger.error("is_session_valid: exception", exc_info=True)
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

        # Wait for JS rendering
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Extract results via JS — more reliable than HTML regex parsing
        # LinkedIn obfuscates class names, but href="/in/..." and data attributes are stable
        results = await self._page.evaluate("""() => {
            const cards = document.querySelectorAll('[data-view-name="search-entity-result-universal-template"]');
            const results = [];
            const SKIP = ['Status is online', 'Status is offline', 'Status is reachable',
                          'Connect', 'Follow', 'Message', 'Pending', 'Send InMail'];

            // Helper: strip "View X's profile" prefix+suffix
            // Handles curly apostrophes (\u2018 \u2019), straight quote, and HTML entities
            function stripViewProfile(s) {
                // First try to extract the name from "View <name>'s profile"
                const m = s.match(/^View\\s+(.+?)[\\u2018\\u2019\\u0027\u2018\u2019']s\\s+profile$/i);
                if (m) return m[1].trim();
                // Fallback: just strip suffixes
                return s.replace(/[\\u2018\\u2019\\u0027\u2018\u2019']s\\s+profile$/i, '')
                        .replace(/^View\\s+/i, '')
                        .trim();
            }
            // Helper: check if text is noise (status indicators, profile view text, buttons, garbage names)
            function isNoise(t) {
                if (SKIP.includes(t)) return true;
                if (/^Status is /i.test(t)) return true;
                if (/^View /i.test(t) && /profile/i.test(t)) return true;
                if (/[\\u2018\\u2019']s\\s+profile$/i.test(t)) return true;
                if (/^Join LinkedIn/i.test(t)) return true;
                if (/^LinkedIn Member/i.test(t)) return true;
                if (/^Sign in/i.test(t)) return true;
                if (/^Provides services/i.test(t)) return true;
                if (/^ACoA/.test(t)) return true;
                if (t.length > 80) return true;
                return false;
            }

            for (const card of cards) {
                const link = card.querySelector('a[href*="/in/"]');
                if (!link) continue;
                const href = link.getAttribute('href') || '';
                const usernameMatch = href.match(/\\/in\\/([^/?]+)/);
                if (!usernameMatch) continue;
                // Skip ACoAA internal IDs — they cannot be deep-screened
                if (/^ACoA/.test(usernameMatch[1])) continue;

                // Get the name from the link's aria-label or span[aria-hidden="true"]
                let name = '';
                const ariaLabel = link.getAttribute('aria-label');
                if (ariaLabel) {
                    name = stripViewProfile(ariaLabel);
                }
                if (!name || isNoise(name)) {
                    name = '';
                    const hiddenSpan = link.querySelector('span[aria-hidden="true"]');
                    if (hiddenSpan) {
                        const t = hiddenSpan.textContent.trim();
                        if (!isNoise(t)) name = t;
                    }
                }

                // Collect meaningful text spans (skip status/buttons)
                const texts = [];
                card.querySelectorAll('span').forEach(s => {
                    const t = s.textContent.trim();
                    if (t && t.length > 2 && t.length < 200
                        && !isNoise(t)
                        && t !== name) {
                        texts.push(t);
                    }
                });

                // Headline is usually the first text after name
                // Location is after connection degree
                let headline = '';
                let location = '';
                let pastDegree = false;
                for (const t of texts) {
                    if (/^\\d+(st|nd|rd|th)$/.test(t) || t.includes('1st') || t.includes('2nd') || t.includes('3rd')) {
                        pastDegree = true;
                        continue;
                    }
                    if (!headline) {
                        headline = t;
                    } else if (pastDegree && !location && t.length > 2) {
                        location = t;
                        break;
                    }
                }

                if (name) {
                    results.push({
                        full_name: name,
                        headline: headline,
                        location: location,
                        linkedin_url: 'https://www.linkedin.com/in/' + usernameMatch[1] + '/',
                        profile_username: usernameMatch[1],
                    });
                }
            }
            return results;
        }""")

        logger.info("Extracted %d results via JS for '%s'", len(results), keywords)
        for i, r in enumerate(results[:3]):
            logger.info("  [%d] %s — %s (%s)", i, r.get("full_name"), r.get("headline", "")[:50], r.get("profile_username"))

        if not results:
            # Fallback: richer extraction from profile links + surrounding context
            logger.info("JS selector found 0, trying enhanced fallback...")
            try:
                visible = await self._page.inner_text("body")
                logger.info("Visible text (500 chars): %s", visible[:500].replace("\n", " | "))

                # Extract profile links with aria-label, clean text, and
                # sibling/parent context for headline extraction
                all_links = await self._page.evaluate("""() => {
                    const SKIP_LINES = [
                        'Status is', 'View ', 'Connect', 'Follow', 'Message',
                        'Pending', 'Send InMail', 'Premium', 'Repost', 'Like',
                        'Comment', 'Share', 'Report', 'Save'
                    ];
                    function isNoise(t) {
                        if (SKIP_LINES.some(s => t.includes(s))) return true;
                        if (/^Join LinkedIn/i.test(t)) return true;
                        if (/^LinkedIn Member/i.test(t)) return true;
                        if (/^Sign in/i.test(t)) return true;
                        if (/^Provides services/i.test(t)) return true;
                        if (/^ACoA/.test(t)) return true;
                        if (t.length > 80) return true;
                        return false;
                    }
                    function cleanLines(raw) {
                        return raw.split('\\n')
                            .map(l => l.trim())
                            .filter(l => l.length > 1 && !isNoise(l));
                    }

                    const links = document.querySelectorAll('a[href*="/in/"]');
                    const results = [];
                    const seenHrefs = new Set();

                    for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/\\/in\\/([^/?]+)/);
                        if (!m) continue;
                        if (seenHrefs.has(m[1])) continue;
                        seenHrefs.add(m[1]);

                        // Strategy 1: aria-label (e.g. "View John Doe's profile")
                        // Handle curly apostrophes (\u2018 \u2019) and straight quotes
                        let name = '';
                        const ariaLabel = a.getAttribute('aria-label') || '';
                        if (ariaLabel) {
                            const am = ariaLabel.match(/^View\\s+(.+?)[\\u2018\\u2019\\u0027\u2018\u2019']s\\s+profile$/i);
                            if (am) {
                                name = am[1].trim();
                            } else {
                                name = ariaLabel
                                    .replace(/[\\u2018\\u2019\\u0027\u2018\u2019']s\\s+profile$/i, '')
                                    .replace(/^View\\s+/i, '')
                                    .trim();
                            }
                        }

                        // Strategy 2: span[aria-hidden="true"] inside the link
                        if (!name) {
                            const hiddenSpan = a.querySelector('span[aria-hidden="true"]');
                            if (hiddenSpan) {
                                const t = hiddenSpan.textContent.trim();
                                if (t && !isNoise(t)) name = t;
                            }
                        }

                        // Strategy 3: textContent lines, skipping noise
                        if (!name) {
                            const lines = cleanLines(a.textContent || '');
                            if (lines.length > 0) name = lines[0];
                        }

                        if (!name || name.length < 2) continue;

                        // Extract headline from nearby context:
                        // Walk up to the closest <li> or container, then grab
                        // non-noise text spans that aren't the name itself
                        let headline = '';
                        const container = a.closest('li') || a.closest('[data-view-name]') || a.parentElement?.parentElement?.parentElement;
                        if (container) {
                            const spans = container.querySelectorAll('span[aria-hidden="true"], span.t-14');
                            for (const s of spans) {
                                const t = s.textContent.trim();
                                if (t && t !== name && t.length > 3 && t.length < 200 && !isNoise(t)
                                    && !/^\\d+(st|nd|rd|th)$/.test(t)) {
                                    headline = t;
                                    break;
                                }
                            }
                        }

                        // Skip ACoAA internal IDs — they cannot be deep-screened
                        if (/^ACoA/.test(m[1])) continue;

                        results.push({
                            username: m[1],
                            name: name,
                            headline: headline
                        });
                    }
                    return results;
                }""")
                logger.info("Found %d profile links via enhanced fallback", len(all_links))

                for link in all_links:
                    results.append({
                        "full_name": link.get("name", ""),
                        "headline": link.get("headline", ""),
                        "location": "",
                        "linkedin_url": f"https://www.linkedin.com/in/{link['username']}/",
                        "profile_username": link["username"],
                    })

                logger.info("Enhanced fallback extracted %d results", len(results))
            except Exception:
                logger.error("Enhanced fallback extraction failed", exc_info=True)

        # Retry logic: if we still got 0 results, reload and try once more
        if not results and not getattr(self, '_search_retried', False):
            self._search_retried = True
            logger.info("Zero results — retrying after 5s reload...")
            await asyncio.sleep(5)
            await self._page.reload(wait_until="domcontentloaded")
            await self._human_scroll()
            await asyncio.sleep(random.uniform(2.0, 4.0))
            results = await self.search_people(keywords, location, page)
            self._search_retried = False
            return results
        self._search_retried = False

        # Final safety net: sanitize all results to catch any garbage that
        # slipped through both extractors (e.g. "Status is offline" as name,
        # "View X's profile" suffixes in any field).
        results = _sanitize_search_results(results)

        return results

    # -------------------------------------------------------------- #
    # Profile
    # -------------------------------------------------------------- #

    async def get_person_profile(self, username: str) -> dict:
        """Fetch and parse a full LinkedIn profile by username.

        Uses ``page.evaluate()`` with JavaScript to extract data directly from
        the DOM.  This approach is resilient to LinkedIn's CSS-class obfuscation
        because it anchors on ``section`` tags, ``h2`` text content, ``data-*``
        attributes, and ``aria-*`` attributes rather than class names.

        Returns a structured dict including experience, education, skills, etc.
        """
        if not self._page:
            raise RuntimeError("Scraper not started; call start() first")

        # ACoAA usernames are internal LinkedIn IDs — they resolve to a
        # "Join LinkedIn" page and waste a rate-limit token.
        if _is_acoa_username(username):
            logger.info(
                "Skipping ACoAA internal ID profile: %s", username
            )
            return {
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
                "error": "acoa_internal_id",
            }

        await self._rate_limiter.acquire("profile")

        # Human-like pre-navigation pause
        await asyncio.sleep(random.uniform(2, 5))

        # Occasionally (1 in 5 calls), add a long pause to simulate distraction
        if random.random() < 0.2:
            long_pause = random.uniform(15, 30)
            logger.info("Human-like long pause: %.1fs", long_pause)
            await asyncio.sleep(long_pause)

        url = f"https://www.linkedin.com/in/{quote(username, safe='')}/"
        logger.info("Fetching LinkedIn profile: %s", username)
        await self._page.goto(url, wait_until="domcontentloaded")

        # Scroll aggressively to trigger lazy-loaded sections (experience,
        # education, skills, etc.) before we attempt extraction.
        for _ in range(6):
            await self._page.mouse.wheel(0, random.randint(400, 700))
            await asyncio.sleep(random.uniform(0.4, 0.9))
        # Scroll back to top so the main profile card is in the viewport
        await self._page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Try clicking "Show all skills" if the button is visible
        try:
            show_skills_btn = await self._page.query_selector(
                'a[href*="/details/skills"], button >> text=/[Ss]how all.*skill/'
            )
            if show_skills_btn and await show_skills_btn.is_visible():
                logger.info("Clicking 'Show all skills' button")
                await show_skills_btn.click()
                await asyncio.sleep(random.uniform(1.5, 2.5))
                # If it navigated to a sub-page, go back after extracting
        except Exception:
            logger.debug("No 'Show all skills' button found or click failed", exc_info=True)

        profile = await self._page.evaluate("""(username) => {
            // ============================================================
            // Helper: find a <section> whose h2 contains the given heading
            // ============================================================
            function findSection(headingText) {
                const sections = document.querySelectorAll('section');
                for (const sec of sections) {
                    const h2 = sec.querySelector('h2');
                    if (!h2) continue;
                    // h2 may wrap text in nested spans; use textContent
                    const txt = (h2.textContent || '').trim();
                    if (txt.toLowerCase().includes(headingText.toLowerCase())) {
                        return sec;
                    }
                }
                // Fallback: look for section with id containing the heading
                for (const sec of sections) {
                    const id = sec.getAttribute('id') || '';
                    if (id.toLowerCase().includes(headingText.toLowerCase())) {
                        return sec;
                    }
                }
                return null;
            }

            // Helper: get visible non-empty text from an element
            function visibleText(el) {
                if (!el) return '';
                return (el.textContent || '').replace(/\\s+/g, ' ').trim();
            }

            // Helper: get all visible <span> texts inside an element, skipping
            // those that only contain whitespace or are sr-only.
            function getSpanTexts(el) {
                if (!el) return [];
                const spans = el.querySelectorAll('span[aria-hidden="true"]');
                const texts = [];
                for (const s of spans) {
                    const t = (s.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t) texts.push(t);
                }
                // If no aria-hidden spans, fall back to all spans
                if (texts.length === 0) {
                    el.querySelectorAll('span').forEach(s => {
                        const t = (s.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t && t.length > 1) texts.push(t);
                    });
                }
                return texts;
            }

            const result = {
                username: username,
                full_name: '',
                headline: '',
                location: '',
                about: '',
                current_company: '',
                current_title: '',
                experience: [],
                education: [],
                skills: [],
                profile_photo_url: ''
            };

            // ---- Name (h1 in the top card / main section) ----
            try {
                const h1 = document.querySelector('h1');
                if (h1) result.full_name = visibleText(h1);
                // Fallback: og:title meta
                if (!result.full_name) {
                    const og = document.querySelector('meta[property="og:title"]');
                    if (og) {
                        result.full_name = (og.getAttribute('content') || '')
                            .replace(/\\s*\\|\\s*LinkedIn.*$/, '').trim();
                    }
                }
            } catch(e) {}

            // ---- Headline (text right below h1) ----
            try {
                // Strategy 1: div.text-body-medium is the most stable selector
                const hDiv = document.querySelector('div.text-body-medium');
                if (hDiv) {
                    result.headline = visibleText(hDiv);
                }
                // Strategy 2: div[data-generated-suggestion-target]
                if (!result.headline) {
                    const sgDiv = document.querySelector('div[data-generated-suggestion-target]');
                    if (sgDiv) result.headline = visibleText(sgDiv);
                }
                // Strategy 3: walk from h1 parent to find the headline div
                if (!result.headline) {
                    const h1 = document.querySelector('h1');
                    if (h1) {
                        let container = h1.closest('section') || h1.closest('div.mt2') || h1.parentElement?.parentElement;
                        if (container) {
                            const divs = container.querySelectorAll('div');
                            for (const d of divs) {
                                const t = visibleText(d);
                                if (t && t !== result.full_name && t.length > 3 && t.length < 300
                                    && !t.includes('connections') && !t.includes('followers')
                                    && !t.includes('Contact info') && !t.includes('mutual')) {
                                    result.headline = t;
                                    break;
                                }
                            }
                        }
                    }
                }
                // Strategy 4: og:description meta often contains the headline
                if (!result.headline) {
                    const ogDesc = document.querySelector('meta[property="og:description"]');
                    if (ogDesc) {
                        const c = (ogDesc.getAttribute('content') || '').split(' - ')[0].trim();
                        if (c && c !== result.full_name) result.headline = c;
                    }
                }
            } catch(e) {}

            // ---- Location ----
            try {
                // Strategy 1: span.text-body-small with inline class (most stable)
                const locSpan = document.querySelector('span.text-body-small.inline.t-black--light');
                if (locSpan) {
                    result.location = visibleText(locSpan);
                }
                // Strategy 2: look for a span with class containing "top-card" + "location"
                if (!result.location) {
                    const locEl = document.querySelector('[class*="top-card"] [class*="location"]')
                        || document.querySelector('[class*="pv-text-details"] span.text-body-small');
                    if (locEl) result.location = visibleText(locEl);
                }
                // Strategy 3: scan spans in the top card for location-like text
                if (!result.location) {
                    const topCard = document.querySelector('h1')?.closest('section')
                        || document.querySelector('h1')?.closest('main')
                        || document.querySelector('main');
                    if (topCard) {
                        const spans = topCard.querySelectorAll('span');
                        for (const s of spans) {
                            const t = (s.textContent || '').trim();
                            if (t && t.length > 3 && t.length < 120
                                && !t.includes('connections') && !t.includes('followers')
                                && !t.includes('Contact info') && !t.includes('mutual')
                                && t !== result.full_name && t !== result.headline) {
                                if (t.includes(',') || /region|area|metro|greater|france|paris|london|new york|singapore|hong kong|bangkok|tokyo|berlin|country|province|state/i.test(t)) {
                                    result.location = t;
                                    break;
                                }
                            }
                        }
                    }
                }
                // Strategy 4: geo.placename meta
                if (!result.location) {
                    const geo = document.querySelector('meta[name="geo.placename"]');
                    if (geo) result.location = geo.getAttribute('content') || '';
                }
                // Strategy 5: og:description often contains "location" after a dash
                if (!result.location) {
                    const ogDesc = document.querySelector('meta[property="og:description"]');
                    if (ogDesc) {
                        const parts = (ogDesc.getAttribute('content') || '').split(' - ');
                        // Last part before "LinkedIn" is often the location
                        for (let i = parts.length - 1; i >= 1; i--) {
                            const p = parts[i].replace(/LinkedIn.*$/, '').trim();
                            if (p && p.length > 2 && p.length < 80) {
                                result.location = p;
                                break;
                            }
                        }
                    }
                }
            } catch(e) {}

            // ---- About ----
            try {
                const aboutSection = findSection('About') || document.querySelector('section#about');
                if (aboutSection) {
                    // Prefer aria-hidden span inside a show-more block
                    const ariaSpan = aboutSection.querySelector('span[aria-hidden="true"]');
                    if (ariaSpan) {
                        result.about = visibleText(ariaSpan);
                    } else {
                        // Fallback: grab all text after the h2
                        const h2 = aboutSection.querySelector('h2');
                        if (h2) {
                            let t = visibleText(aboutSection);
                            const heading = visibleText(h2);
                            t = t.replace(heading, '').trim();
                            result.about = t;
                        }
                    }
                }
            } catch(e) {}

            // ---- Profile photo ----
            try {
                // Profile photo img usually has alt containing the person's name
                const name = result.full_name;
                if (name) {
                    const imgs = document.querySelectorAll('img');
                    for (const img of imgs) {
                        const alt = img.getAttribute('alt') || '';
                        if (alt.includes(name) && img.src && img.src.startsWith('http')) {
                            result.profile_photo_url = img.src;
                            break;
                        }
                    }
                }
                // Fallback: og:image
                if (!result.profile_photo_url) {
                    const ogImg = document.querySelector('meta[property="og:image"]');
                    if (ogImg) result.profile_photo_url = ogImg.getAttribute('content') || '';
                }
            } catch(e) {}

            // ---- Experience ----
            try {
                const expSection = findSection('Experience') || document.querySelector('section#experience');
                if (expSection) {
                    const items = expSection.querySelectorAll('li');
                    for (const li of items) {
                        const spans = getSpanTexts(li);
                        if (spans.length === 0) continue;

                        // Heuristic: first span is usually the title, second is
                        // company (sometimes with " · Full-time"), third is duration
                        let title = spans[0] || '';
                        let company = spans.length >= 2 ? spans[1] : '';
                        let duration = spans.length >= 3 ? spans[2] : '';
                        let description = '';

                        // Clean company: remove employment type suffix
                        company = company.replace(/\\s*·\\s*(Full-time|Part-time|Contract|Freelance|Internship|Self-employed|Seasonal|Apprenticeship).*$/i, '').trim();

                        // If the title looks like a company name (contains "·"), swap
                        if (title.includes(' · ')) {
                            const parts = title.split(' · ');
                            title = parts[0].trim();
                        }

                        // Look for a longer text block that might be a description
                        const descSpan = li.querySelector('[class*="inline-show-more-text"] span[aria-hidden="true"]');
                        if (descSpan) {
                            description = visibleText(descSpan);
                        }

                        // Skip noise items (endorsements, "Show all" links, etc.)
                        if (!title || /^\\d+$/.test(title) || /show (all|more)/i.test(title)) continue;

                        result.experience.push({
                            company: company,
                            title: title,
                            duration: duration,
                            description: description
                        });
                    }

                    // Deduplicate by title+company
                    const seen = new Set();
                    result.experience = result.experience.filter(e => {
                        const key = (e.title + '|' + e.company).toLowerCase();
                        if (seen.has(key)) return false;
                        seen.add(key);
                        return true;
                    });
                }
            } catch(e) {}

            // Populate current company/title from first experience
            if (result.experience.length > 0) {
                result.current_company = result.experience[0].company;
                result.current_title = result.experience[0].title;
            }

            // ---- Education ----
            try {
                const eduSection = findSection('Education') || document.querySelector('section#education');
                if (eduSection) {
                    const items = eduSection.querySelectorAll('li');
                    for (const li of items) {
                        const spans = getSpanTexts(li);
                        if (spans.length === 0) continue;

                        let school = spans[0] || '';
                        let degree = spans.length >= 2 ? spans[1] : '';
                        let field = '';
                        let years = '';

                        // Degree + field may be combined: "Bachelor's degree, CS"
                        if (degree.includes(',')) {
                            const parts = degree.split(',', 2);
                            degree = parts[0].trim();
                            field = parts[1].trim();
                        }

                        // Look for year range in the remaining spans
                        for (let i = 2; i < spans.length; i++) {
                            const yrMatch = spans[i].match(/(\\d{4})\\s*[-–]\\s*(\\d{4}|Present)/);
                            if (yrMatch) {
                                years = yrMatch[1] + ' - ' + yrMatch[2];
                                break;
                            }
                        }

                        // Skip noise
                        if (!school || /^\\d+$/.test(school) || /show (all|more)/i.test(school)) continue;

                        result.education.push({
                            school: school,
                            degree: degree,
                            field: field,
                            years: years
                        });
                    }

                    // Deduplicate
                    const seen = new Set();
                    result.education = result.education.filter(e => {
                        const key = (e.school + '|' + e.degree).toLowerCase();
                        if (seen.has(key)) return false;
                        seen.add(key);
                        return true;
                    });
                }
            } catch(e) {}

            // ---- Skills ----
            try {
                const skillsSection = findSection('Skills') || document.querySelector('section#skills');
                if (skillsSection) {
                    const spans = getSpanTexts(skillsSection);
                    const seen = new Set();
                    const noise = /^\\d+$|endorse|see\\s+all|show\\s+(more|less)|^skill$/i;
                    for (const s of spans) {
                        const t = s.trim();
                        if (t && !seen.has(t.toLowerCase()) && !noise.test(t)
                            && t.length < 100 && t !== 'Skills') {
                            seen.add(t.toLowerCase());
                            result.skills.push(t);
                        }
                    }
                }
            } catch(e) {}

            return result;
        }""", username)

        # Human-like post-extraction pause
        await asyncio.sleep(random.uniform(1, 3))

        # Log extraction results for each field
        logger.info("Extracted profile for %s (%s)", profile.get("full_name", "?"), username)
        logger.info("  headline: %s", (profile.get("headline") or "")[:80])
        logger.info("  location: %s", profile.get("location") or "(empty)")
        logger.info("  about: %s", (profile.get("about") or "")[:100] + ("..." if len(profile.get("about") or "") > 100 else ""))
        logger.info("  current_company: %s", profile.get("current_company") or "(empty)")
        logger.info("  current_title: %s", profile.get("current_title") or "(empty)")
        logger.info("  experience: %d entries", len(profile.get("experience", [])))
        logger.info("  education: %d entries", len(profile.get("education", [])))
        logger.info("  skills: %d entries — %s", len(profile.get("skills", [])), ", ".join(profile.get("skills", [])[:5]))
        logger.info("  profile_photo_url: %s", "yes" if profile.get("profile_photo_url") else "(empty)")

        return profile

    # -------------------------------------------------------------- #
    # Company enrichment
    # -------------------------------------------------------------- #

    async def get_company_info(self, company_name: str) -> dict:
        """Fetch company information from its LinkedIn page.

        Navigates to the company's LinkedIn page and extracts structured data
        including size, industry, description, and headquarters.

        Returns a dict with keys:
            name, linkedin_url, industry, company_size, description,
            headquarters, website, founded, specialties
        """
        if not self._page:
            raise RuntimeError("Scraper not started; call start() first")

        await self._rate_limiter.acquire("profile")

        # Build the company search URL — LinkedIn company pages use /company/<slug>
        # We try the slugified company name first; if not found we search
        slug = (
            company_name.lower()
            .replace(" ", "-")
            .replace(",", "")
            .replace(".", "")
            .replace("&", "and")
        )
        # Remove double dashes
        while "--" in slug:
            slug = slug.replace("--", "-")
        slug = slug.strip("-")

        company_url = f"https://www.linkedin.com/company/{quote(slug, safe='-')}/about/"
        logger.info("Fetching company info: %s → %s", company_name, company_url)

        await self._page.goto(company_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Check if we landed on a valid company page or got redirected
        current_url = self._page.url
        if "/company/" not in current_url:
            logger.warning("Company page redirect — not a valid company URL: %s", current_url)
            return {"name": company_name, "error": "company_not_found"}

        # Scroll to load lazy content
        for _ in range(3):
            await self._page.mouse.wheel(0, random.randint(300, 500))
            await asyncio.sleep(random.uniform(0.3, 0.6))
        await self._page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(1.0, 2.0))

        company = await self._page.evaluate("""(companyName) => {
            function visibleText(el) {
                if (!el) return '';
                return (el.textContent || '').replace(/\\s+/g, ' ').trim();
            }

            const result = {
                name: companyName,
                linkedin_url: window.location.href.replace(/\\/about\\/?$/, '/'),
                industry: '',
                company_size: '',
                description: '',
                headquarters: '',
                website: '',
                founded: '',
                specialties: ''
            };

            // ---- Company name from the page (more authoritative) ----
            try {
                const h1 = document.querySelector('h1');
                if (h1) {
                    const t = visibleText(h1);
                    if (t) result.name = t;
                }
            } catch(e) {}

            // ---- Description / overview ----
            try {
                // The about section often has a <p> or <span> with the description
                const aboutSection = document.querySelector('section.org-about-module')
                    || document.querySelector('[class*="about-us"]')
                    || document.querySelector('section[data-test-id="about-us"]');
                if (aboutSection) {
                    const p = aboutSection.querySelector('p')
                        || aboutSection.querySelector('span[aria-hidden="true"]')
                        || aboutSection.querySelector('div[class*="break-words"]');
                    if (p) result.description = visibleText(p);
                }
                // Fallback: og:description
                if (!result.description) {
                    const og = document.querySelector('meta[property="og:description"]');
                    if (og) result.description = (og.getAttribute('content') || '').trim();
                }
            } catch(e) {}

            // ---- Structured details (dt/dd pairs on the /about/ page) ----
            try {
                // LinkedIn company about pages use <dt>Label</dt><dd>Value</dd>
                const dts = document.querySelectorAll('dt');
                for (const dt of dts) {
                    const label = visibleText(dt).toLowerCase();
                    const dd = dt.nextElementSibling;
                    if (!dd) continue;
                    const value = visibleText(dd);
                    if (!value) continue;

                    if (label.includes('website')) {
                        result.website = value;
                    } else if (label.includes('industry')) {
                        result.industry = value;
                    } else if (label.includes('company size') || label.includes('taille')) {
                        result.company_size = value;
                    } else if (label.includes('headquarters') || label.includes('siege')
                               || label.includes('siège')) {
                        result.headquarters = value;
                    } else if (label.includes('founded') || label.includes('fond')) {
                        result.founded = value;
                    } else if (label.includes('specialties') || label.includes('spécialisations')
                               || label.includes('specialisations')) {
                        result.specialties = value;
                    }
                }
            } catch(e) {}

            // ---- Fallback: parse from the sidebar / header area ----
            try {
                if (!result.industry) {
                    const indEl = document.querySelector('[class*="org-top-card-summary-info-list"] [class*="industry"]')
                        || document.querySelector('[class*="top-card"] [class*="industry"]');
                    if (indEl) result.industry = visibleText(indEl);
                }
                if (!result.company_size) {
                    const sizeEl = document.querySelector('[class*="org-top-card-summary-info-list"] [class*="company-size"]');
                    if (sizeEl) result.company_size = visibleText(sizeEl);
                }
                // Company size sometimes appears in text like "1,001-5,000 employees"
                if (!result.company_size) {
                    const allText = document.body.innerText || '';
                    const sizeMatch = allText.match(/(\\d[\\d,]+-\\d[\\d,]+)\\s*employees/i)
                        || allText.match(/(\\d[\\d,]+\\+?)\\s*employees/i);
                    if (sizeMatch) result.company_size = sizeMatch[0];
                }
            } catch(e) {}

            return result;
        }""", company_name)

        logger.info("Company info for '%s': industry=%s, size=%s, hq=%s",
                     company.get("name"), company.get("industry") or "(empty)",
                     company.get("company_size") or "(empty)",
                     company.get("headquarters") or "(empty)")

        return company

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
            await asyncio.sleep(random.uniform(1.0, 3.0))
        # Occasionally scroll back up a tiny bit
        if random.random() < 0.3:
            await self._page.mouse.wheel(0, -random.randint(50, 150))
            await asyncio.sleep(random.uniform(0.5, 1.5))
