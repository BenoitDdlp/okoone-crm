#!/bin/bash
set -e
cd ~/okoone-crm
git pull origin main
.venv/bin/pip install -r requirements.txt --quiet
.venv/bin/pytest tests/unit -q || { echo "Tests failed, aborting deploy"; exit 1; }
systemctl --user restart okoone-crm.service
echo "Deployed at $(date)"
