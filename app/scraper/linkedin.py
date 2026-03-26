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

from app.scraper.parser import parse_search_results
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

            for (const card of cards) {
                const link = card.querySelector('a[href*="/in/"]');
                if (!link) continue;
                const href = link.getAttribute('href') || '';
                const usernameMatch = href.match(/\\/in\\/([^/?]+)/);
                if (!usernameMatch) continue;

                // Get the name from the link's aria-label or span[aria-hidden="true"]
                let name = '';
                const ariaLabel = link.getAttribute('aria-label');
                if (ariaLabel) {
                    name = ariaLabel.replace(/View .+'s profile$/, '').replace(/'s profile$/, '').trim();
                }
                if (!name) {
                    const hiddenSpan = link.querySelector('span[aria-hidden="true"]');
                    if (hiddenSpan) name = hiddenSpan.textContent.trim();
                }

                // Collect meaningful text spans (skip status/buttons)
                const texts = [];
                card.querySelectorAll('span').forEach(s => {
                    const t = s.textContent.trim();
                    if (t && t.length > 2 && t.length < 200
                        && !SKIP.includes(t)
                        && !t.startsWith('View ')
                        && !t.includes("'s profile")
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
            # Fallback: parse from visible text + href links (same approach as LinkedIn MCP)
            logger.info("JS selector found 0, trying text+links fallback...")
            try:
                visible = await self._page.inner_text("body")
                logger.info("Visible text (500 chars): %s", visible[:500].replace("\n", " | "))

                # Extract all /in/ links from the page
                all_links = await self._page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href*="/in/"]'))
                        .map(a => ({href: a.href, text: a.textContent.trim()}))
                        .filter(a => a.text.length > 0 && !a.text.includes('Premium'));
                }""")
                logger.info("Found %d profile links via fallback", len(all_links))

                seen = set()
                for link in all_links:
                    import re as _re
                    m = _re.search(r"/in/([^/?]+)", link.get("href", ""))
                    if not m:
                        continue
                    username = m.group(1)
                    if username in seen:
                        continue
                    seen.add(username)

                    # Parse name from link text
                    name = link.get("text", "").split("\n")[0].strip()
                    name = _re.sub(r"View .+'s profile$", "", name).strip()
                    if not name or len(name) < 2:
                        continue

                    results.append({
                        "full_name": name,
                        "headline": "",
                        "location": "",
                        "linkedin_url": f"https://www.linkedin.com/in/{username}/",
                        "profile_username": username,
                    })

                logger.info("Fallback extracted %d results", len(results))
            except Exception:
                logger.error("Fallback extraction failed", exc_info=True)

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

        await self._rate_limiter.acquire("profile")

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
                // The headline is typically in a div sibling just below h1.
                // Strategy: find h1's parent, then look for the next div
                const h1 = document.querySelector('h1');
                if (h1) {
                    // Walk siblings of h1 or its parent
                    let container = h1.parentElement;
                    if (container) {
                        const divs = container.parentElement
                            ? container.parentElement.querySelectorAll('div')
                            : container.querySelectorAll('div');
                        for (const d of divs) {
                            const t = visibleText(d);
                            if (t && t !== result.full_name && t.length > 3 && t.length < 300) {
                                // Skip if it looks like a location or connections line
                                if (t.includes('connections') || t.includes('followers')) continue;
                                result.headline = t;
                                break;
                            }
                        }
                    }
                }
                // Fallback: look for div[data-generated-suggestion-target]
                if (!result.headline) {
                    const hDiv = document.querySelector('div[data-generated-suggestion-target]');
                    if (hDiv) result.headline = visibleText(hDiv);
                }
            } catch(e) {}

            // ---- Location ----
            try {
                // Location often sits in a span near h1's section, sometimes
                // with class containing "location" or near a map-pin svg.
                const topCard = document.querySelector('h1')?.closest('section')
                    || document.querySelector('h1')?.closest('main')
                    || document.querySelector('main');
                if (topCard) {
                    // Look for a span whose text looks like a location (not
                    // connections/followers). LinkedIn sometimes uses a span
                    // right after the headline area.
                    const spans = topCard.querySelectorAll('span');
                    for (const s of spans) {
                        const t = (s.textContent || '').trim();
                        // Location patterns: "City, Country" or "Greater X Area"
                        if (t && t.length > 3 && t.length < 120
                            && !t.includes('connections') && !t.includes('followers')
                            && !t.includes('Contact info')
                            && t !== result.full_name && t !== result.headline) {
                            // Heuristic: contains a comma or known geo terms
                            if (t.includes(',') || /region|area|france|paris|london|new york|singapore|city|country/i.test(t)) {
                                result.location = t;
                                break;
                            }
                        }
                    }
                }
                // Fallback: look for geo_location in meta or JSON-LD
                if (!result.location) {
                    const geo = document.querySelector('meta[name="geo.placename"]');
                    if (geo) result.location = geo.getAttribute('content') || '';
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
