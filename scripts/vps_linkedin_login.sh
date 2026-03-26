#!/bin/bash
# =============================================================================
# LinkedIn Re-Authentication Script for VPS
# =============================================================================
# Run this when the CRM dashboard shows "Session LinkedIn expiree"
#
# Usage (from your LOCAL machine):
#   ssh -L 6080:127.0.0.1:6080 -i ~/.ssh/vps_key openclaw@46.250.239.50 \
#       'bash ~/okoone-crm/scripts/vps_linkedin_login.sh'
#
# Then open http://localhost:6080/vnc.html in your browser
# Log into LinkedIn, wait for redirect to /feed/, then Ctrl+C
# =============================================================================

set -e

echo "=== LinkedIn Re-Auth for Okoone CRM ==="
echo ""

# 1. Stop CRM (releases the browser profile)
echo "[1/5] Stopping CRM service..."
systemctl --user stop okoone-crm.service 2>/dev/null || true
sleep 1

# 2. Kill any leftover Chrome processes using the profile
echo "[2/5] Cleaning up stale processes..."
pkill -f "chrome.*okoone-linkedin" 2>/dev/null || true
rm -f /home/openclaw/.okoone-linkedin/profile/SingletonLock
rm -f /home/openclaw/.okoone-linkedin/profile/SingletonCookie
rm -f /home/openclaw/.okoone-linkedin/profile/SingletonSocket
sleep 1

# 3. Start virtual display + VNC (localhost only)
echo "[3/5] Starting VNC (localhost:6080 only)..."
pkill -f Xvfb 2>/dev/null || true
pkill -f x11vnc 2>/dev/null || true
pkill -f websockify 2>/dev/null || true
sleep 1

Xvfb :99 -screen 0 1280x900x24 &>/dev/null &
sleep 1
x11vnc -display :99 -nopw -listen 127.0.0.1 -rfbport 5900 -bg -q
websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 &>/dev/null &
sleep 1

echo ""
echo "============================================"
echo "  Open http://localhost:6080/vnc.html"
echo "  (requires SSH tunnel: ssh -L 6080:127.0.0.1:6080)"
echo "============================================"
echo ""

# 4. Launch Chromium on LinkedIn login
echo "[4/5] Opening LinkedIn login page..."
DISPLAY=:99 /home/openclaw/okoone-crm/.venv/bin/python3 -c "
import asyncio
from patchright.async_api import async_playwright
async def login():
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir='/home/openclaw/.okoone-linkedin/profile',
        headless=False,
        args=['--disable-blink-features=AutomationControlled','--no-sandbox','--disable-gpu'],
        ignore_default_args=['--enable-automation'],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto('https://www.linkedin.com/login')
    print()
    print('>> Login dans le navigateur VNC, puis attends le redirect vers /feed/')
    print('>> Appuie Ctrl+C quand tu es connecte')
    print()
    try:
        await page.wait_for_url('**/feed/**', timeout=300000)
        print('LOGIN SUCCESS - session sauvegardee!')
    except KeyboardInterrupt:
        print('Interrupted - saving session anyway...')
    except Exception as e:
        print(f'Timeout/error: {e}')
    await ctx.close()
    await pw.stop()
asyncio.run(login())
"

# 5. Cleanup and restart CRM
echo "[5/5] Cleaning up VNC and restarting CRM..."
pkill -f Xvfb 2>/dev/null || true
pkill -f x11vnc 2>/dev/null || true
pkill -f websockify 2>/dev/null || true
systemctl --user start okoone-crm.service
sleep 3

if curl -s localhost:4567/health | grep -q '"ok"'; then
    echo ""
    echo "=== CRM running. Session LinkedIn refreshed! ==="
else
    echo ""
    echo "=== WARNING: CRM not healthy, check logs ==="
    journalctl --user -u okoone-crm.service --no-pager -n 5
fi
