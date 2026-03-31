#!/usr/bin/env python3
"""Auto-heal script for Okoone CRM.

Runs via cron every 15 minutes. Checks:
1. CRM service is running
2. LinkedIn session is valid
3. If expired, attempts auto-relogin via Welcome Back
4. If relogin fails, logs the failure (manual intervention needed)

Usage: python3 scripts/auto_heal.py
Cron:  */15 * * * * cd /home/openclaw/okoone-crm && .venv/bin/python3 scripts/auto_heal.py >> /tmp/auto_heal.log 2>&1
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime

LOG_PREFIX = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
PROFILE_DIR = "/home/openclaw/.okoone-linkedin/profile"
CRM_URL = "http://localhost:4567/health"
LOCK_FILES = ["SingletonLock", "SingletonCookie", "SingletonSocket"]


def log(msg: str) -> None:
    print(f"[{LOG_PREFIX}] {msg}", flush=True)


def check_crm_running() -> bool:
    """Check if CRM service is running and responding."""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "5", CRM_URL],
            capture_output=True, text=True, timeout=10,
        )
        return '"ok"' in result.stdout
    except Exception:
        return False


def restart_crm() -> bool:
    """Restart the CRM systemd service."""
    try:
        subprocess.run(["systemctl", "--user", "restart", "okoone-crm.service"],
                       capture_output=True, timeout=15)
        import time
        time.sleep(5)
        return check_crm_running()
    except Exception:
        return False


def remove_locks() -> None:
    """Remove stale browser lock files."""
    for lock in LOCK_FILES:
        path = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(path):
            os.unlink(path)
            log(f"Removed lock: {lock}")


async def check_and_fix_session() -> bool:
    """Check LinkedIn session and auto-relogin if needed."""
    from patchright.async_api import async_playwright

    remove_locks()

    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Check session
        await page.goto("https://www.linkedin.com/feed/",
                        wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        url = page.url

        if "/feed" in url and "/login" not in url:
            log("LinkedIn session: VALID")
            await ctx.close()
            return True

        log("LinkedIn session: EXPIRED — attempting auto-relogin...")

        # Try Welcome Back
        await page.goto("https://www.linkedin.com/login",
                        wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        body = await page.inner_text("body")

        if "Welcome Back" in body or "Welcome back" in body:
            cards = await page.query_selector_all("div[role='button'], button, a")
            for card in cards:
                text = (await card.text_content() or "").lower()
                if "gmail" in text or "benoit" in text or "ddlp" in text:
                    log("Clicking Welcome Back account card...")
                    await card.click()
                    await asyncio.sleep(8)
                    break

            if "/feed" in page.url:
                log("AUTO-RELOGIN SUCCESS via Welcome Back")
                await ctx.close()
                return True

        # Try credential login (with fresh profile if needed)
        log("Welcome Back failed — trying credentials...")
        await ctx.close()
        await pw.stop()

        # If login page doesn't even load, the profile is corrupted.
        # Nuke it and start fresh.
        log("Nuking corrupted profile and starting fresh...")
        import shutil
        if os.path.exists(PROFILE_DIR):
            shutil.rmtree(PROFILE_DIR)
        os.makedirs(PROFILE_DIR, exist_ok=True)

        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await page.goto("https://www.linkedin.com/login",
                            wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            await page.fill("#username", "benoitddlp82@gmail.com")
            await asyncio.sleep(0.5)
            await page.evaluate("""() => {
                document.getElementById("password").value = "$P53qLop34Cq#skN";
                document.getElementById("password").dispatchEvent(new Event("input", {bubbles: true}));
            }""")
            await asyncio.sleep(0.3)
            await page.click("button[type=submit]")
            await asyncio.sleep(8)

            if "/feed" in page.url:
                log("AUTO-RELOGIN SUCCESS via credentials")
                await ctx.close()
                return True

            log(f"Credential login failed — post-login URL: {page.url}")
            # Check for security challenge
            if "challenge" in page.url or "checkpoint" in page.url:
                log("SECURITY CHALLENGE detected — manual intervention required")
        except Exception as e:
            log(f"Credential login error: {e}")

        await ctx.close()
        return False

    except Exception as e:
        log(f"Session check error: {e}")
        return False
    finally:
        await pw.stop()


def main() -> None:
    log("=== Auto-heal check ===")

    # 1. Check CRM is running
    if not check_crm_running():
        log("CRM is DOWN — restarting...")
        if restart_crm():
            log("CRM restarted successfully")
        else:
            log("CRM RESTART FAILED — manual intervention needed")
            sys.exit(1)
    else:
        log("CRM: running")

    # 2. Stop CRM to release browser profile
    subprocess.run(["systemctl", "--user", "stop", "okoone-crm.service"],
                   capture_output=True, timeout=10)
    import time
    time.sleep(2)

    # 3. Check and fix LinkedIn session
    session_ok = asyncio.run(check_and_fix_session())

    # 4. Clean up locks and restart CRM
    remove_locks()
    time.sleep(1)
    subprocess.run(["systemctl", "--user", "start", "okoone-crm.service"],
                   capture_output=True, timeout=10)
    time.sleep(3)

    if check_crm_running():
        log(f"CRM restarted — session {'VALID' if session_ok else 'EXPIRED (needs manual fix)'}")
    else:
        log("CRM FAILED TO START after session check")

    log("=== Done ===\n")


if __name__ == "__main__":
    main()
