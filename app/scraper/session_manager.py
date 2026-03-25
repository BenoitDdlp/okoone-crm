"""Manage LinkedIn browser session persistence and health checks.

Cookies are encrypted at rest with Fernet (symmetric AES-128-CBC) and
stored in the ``linkedin_sessions`` table so sessions survive container
restarts without requiring a fresh manual login every time.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from app.scraper.linkedin import LinkedInScraper

logger = logging.getLogger(__name__)


class SessionManager:
    """Manage LinkedIn browser session persistence and health checks.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    fernet_key:
        URL-safe base64-encoded 32-byte Fernet key for cookie encryption.
    profile_dir:
        Path to the Chromium persistent-profile directory.
    session_name:
        Logical name for this session row (default ``"primary"``).
    """

    def __init__(
        self,
        db_path: str,
        fernet_key: str,
        profile_dir: str,
        session_name: str = "primary",
    ) -> None:
        self._db_path = db_path
        self._fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
        self._profile_dir = profile_dir
        self._session_name = session_name

    # -------------------------------------------------------------- #
    # Health
    # -------------------------------------------------------------- #

    async def check_health(self, scraper: LinkedInScraper) -> bool:
        """Check if the current browser session is valid.

        Returns ``True`` when LinkedIn does not redirect to login/checkpoint.
        On failure the method logs a warning but never raises.
        """
        try:
            is_valid = await scraper.is_session_valid()
            if is_valid:
                logger.info("LinkedIn session '%s' is healthy", self._session_name)
            else:
                logger.warning(
                    "LinkedIn session '%s' is expired or blocked",
                    self._session_name,
                )
            return is_valid
        except Exception:
            logger.warning("Session health check raised an exception", exc_info=True)
            return False

    # -------------------------------------------------------------- #
    # Cookie persistence
    # -------------------------------------------------------------- #

    async def save_cookies(self, scraper: LinkedInScraper) -> None:
        """Export cookies from the browser context, encrypt, and store in DB."""
        cookies = await scraper.get_cookies()
        if not cookies:
            logger.warning("No cookies to save for session '%s'", self._session_name)
            return

        plaintext = json.dumps(cookies, separators=(",", ":")).encode()
        encrypted = self._fernet.encrypt(plaintext).decode()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        async with aiosqlite.connect(self._db_path) as db:
            existing = await db.execute_fetchall(
                "SELECT id FROM linkedin_sessions WHERE session_name = ?",
                (self._session_name,),
            )
            if existing:
                await db.execute(
                    """
                    UPDATE linkedin_sessions
                       SET cookies_json = ?, last_used_at = ?, is_active = 1
                     WHERE session_name = ?
                    """,
                    (encrypted, now, self._session_name),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO linkedin_sessions
                        (session_name, cookies_json, user_agent, is_active, last_used_at)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (self._session_name, encrypted, "", now),
                )
            await db.commit()

        logger.info(
            "Saved %d cookies for session '%s'",
            len(cookies),
            self._session_name,
        )

    async def restore_cookies(self, scraper: LinkedInScraper) -> bool:
        """Load encrypted cookies from DB and inject into browser context.

        Picks the most recent active session regardless of name.
        Returns ``True`` if cookies were successfully restored.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute_fetchall(
                """
                SELECT cookies_json, session_name FROM linkedin_sessions
                 WHERE is_active = 1
                 ORDER BY last_used_at DESC, created_at DESC LIMIT 1
                """,
            )

        if not row:
            logger.info("No active LinkedIn session found in DB")
            return False

        encrypted: str = row[0]["cookies_json"]
        found_name: str = row[0]["session_name"]
        if not encrypted:
            logger.info("Session '%s' has empty cookie data", found_name)
            return False

        try:
            plaintext = self._fernet.decrypt(encrypted.encode())
            cookies: list[dict] = json.loads(plaintext)
        except InvalidToken:
            logger.error(
                "Failed to decrypt cookies for session '%s' (bad key?)",
                self._session_name,
            )
            return False
        except json.JSONDecodeError:
            logger.error(
                "Decrypted cookie data for session '%s' is not valid JSON",
                self._session_name,
            )
            return False

        await scraper.set_cookies(cookies)
        logger.info(
            "Restored %d cookies for session '%s'",
            len(cookies),
            self._session_name,
        )
        return True

    # -------------------------------------------------------------- #
    # Interactive (manual) login
    # -------------------------------------------------------------- #

    async def interactive_login(self) -> None:
        """Launch a *visible* browser window for manual LinkedIn login.

        Intended to be invoked from a terminal (e.g. over SSH with X
        forwarding or on a local desktop).  The method opens LinkedIn's
        login page, waits for the operator to complete authentication,
        then persists the resulting cookies.
        """
        from patchright.async_api import async_playwright

        logger.info("Starting interactive login for session '%s'", self._session_name)

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch_persistent_context(
                user_data_dir=self._profile_dir,
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            page = await browser.new_page()
            await page.goto(
                "https://www.linkedin.com/login", wait_until="domcontentloaded"
            )

            # Wait until the user finishes login (detected by the URL leaving /login)
            logger.info(
                "Waiting for manual login... Complete the login in the browser window."
            )
            try:
                await page.wait_for_url(
                    lambda url: "/feed" in url or "/mynetwork" in url,
                    timeout=300_000,  # 5 minutes
                )
            except Exception:
                logger.warning(
                    "Timed out waiting for login redirect. "
                    "Will still attempt to save cookies."
                )

            # Extract cookies and save via a temporary scraper-like interface
            cookies = await browser.cookies()
            if cookies:
                plaintext = json.dumps(cookies, separators=(",", ":")).encode()
                encrypted = self._fernet.encrypt(plaintext).decode()
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                async with aiosqlite.connect(self._db_path) as db:
                    existing = await db.execute_fetchall(
                        "SELECT id FROM linkedin_sessions WHERE session_name = ?",
                        (self._session_name,),
                    )
                    if existing:
                        await db.execute(
                            """
                            UPDATE linkedin_sessions
                               SET cookies_json = ?, last_used_at = ?, is_active = 1
                             WHERE session_name = ?
                            """,
                            (encrypted, now, self._session_name),
                        )
                    else:
                        await db.execute(
                            """
                            INSERT INTO linkedin_sessions
                                (session_name, cookies_json, user_agent, is_active, last_used_at)
                            VALUES (?, ?, ?, 1, ?)
                            """,
                            (self._session_name, encrypted, "", now),
                        )
                    await db.commit()

                logger.info(
                    "Interactive login complete. Saved %d cookies for '%s'.",
                    len(cookies),
                    self._session_name,
                )
            else:
                logger.warning("No cookies obtained after interactive login")

            await browser.close()
        finally:
            await pw.stop()

    # -------------------------------------------------------------- #
    # Invalidation
    # -------------------------------------------------------------- #

    async def invalidate(self) -> None:
        """Mark the stored session as inactive."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE linkedin_sessions SET is_active = 0 WHERE session_name = ?",
                (self._session_name,),
            )
            await db.commit()
        logger.info("Session '%s' marked as inactive", self._session_name)
