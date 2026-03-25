#!/bin/bash
# vps/update.sh — Pull latest code from GitHub and restart services
# Usage: bash /opt/polymarket-engine/vps/update.sh
set -euo pipefail
APP_DIR="/opt/polymarket-engine"

echo "=== Pulling latest code ==="
cd $APP_DIR
sudo -u polymarket git pull origin main

echo "=== Updating Python dependencies ==="
sudo -u polymarket $APP_DIR/venv/bin/pip install -r requirements-scan.txt -q

echo "=== Reloading systemd services ==="
systemctl daemon-reload
systemctl restart polymarket-scan.timer polymarket-resolve.timer

echo "=== Status ==="
systemctl status polymarket-scan.timer --no-pager
systemctl status polymarket-resolve.timer --no-pager
echo "Update complete."