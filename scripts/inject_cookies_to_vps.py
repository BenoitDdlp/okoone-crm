"""Inject LinkedIn cookies into the CRM database on the VPS.

Usage: python scripts/inject_cookies_to_vps.py <cookies_json_file>
Reads cookies JSON, encrypts with Fernet, inserts into linkedin_sessions table via SSH.
"""

import json
import subprocess
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/inject_cookies_to_vps.py <cookies.json>")
        sys.exit(1)

    cookies_file = Path(sys.argv[1])
    cookies_data = cookies_file.read_text()

    # Validate JSON
    cookies = json.loads(cookies_data)
    print(f"Loaded {len(cookies)} cookies")

    # Build Python one-liner to run on VPS: encrypt cookies and insert into DB
    remote_script = f"""
import json, sqlite3, os, sys
sys.path.insert(0, '/home/openclaw/okoone-crm')
from cryptography.fernet import Fernet

cookies_json = '''{cookies_data}'''
fernet_key = None
with open('/home/openclaw/okoone-crm/.env') as f:
    for line in f:
        if line.startswith('FERNET_KEY='):
            fernet_key = line.strip().split('=', 1)[1]

if not fernet_key:
    print('FERNET_KEY not found in .env')
    sys.exit(1)

f = Fernet(fernet_key.encode())
encrypted = f.encrypt(cookies_json.encode()).decode()

db = sqlite3.connect('/home/openclaw/okoone-crm/db/okoone_crm.sqlite')
db.execute('DELETE FROM linkedin_sessions WHERE session_name = ?', ('benoit-main',))
db.execute(
    'INSERT INTO linkedin_sessions (session_name, cookies_json, user_agent, is_active) VALUES (?, ?, ?, 1)',
    ('benoit-main', encrypted, 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36')
)
db.commit()
db.close()
print('Cookies injected successfully')
"""

    result = subprocess.run(
        ["ssh", "-i", str(Path.home() / ".ssh" / "vps_key"), "openclaw@46.250.239.50",
         f"cd ~/okoone-crm && .venv/bin/python3 -c {repr(remote_script)}"],
        capture_output=True, text=True
    )

    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
