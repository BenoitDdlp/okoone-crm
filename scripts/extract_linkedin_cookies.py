"""Extract ALL LinkedIn cookies (including httpOnly) via CDP.

Usage: python scripts/extract_linkedin_cookies.py
"""

import asyncio
import json
import sys
from pathlib import Path


async def main():
    from patchright.async_api import async_playwright

    profile_dir = str(Path.home() / ".linkedin-mcp" / "profile")

    if not Path(profile_dir).exists():
        print("Profile directory not found.", file=sys.stderr)
        sys.exit(1)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=True,
    )

    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15000)
    await asyncio.sleep(2)

    # Use CDP to get ALL cookies including httpOnly
    cdp = await context.new_cdp_session(page)
    result = await cdp.send("Network.getAllCookies")
    all_cookies = result.get("cookies", [])

    linkedin_cookies = [c for c in all_cookies if "linkedin" in c.get("domain", "")]

    await cdp.detach()
    await context.close()
    await pw.stop()

    print(json.dumps(linkedin_cookies))


if __name__ == "__main__":
    asyncio.run(main())
