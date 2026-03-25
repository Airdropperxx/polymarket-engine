#!/bin/bash
# vps/setup.sh — Run once on a fresh Ubuntu 22.04 VPS as root
# Usage: bash vps/setup.sh
set -euo pipefail

REPO_URL="https://github.com/Airdropperxx/polymarket-engine.git"
APP_DIR="/opt/polymarket-engine"
APP_USER="polymarket"
PYTHON="python3.11"
LOG_DIR="/var/log/polymarket"

echo "=== [1/8] System update ==="
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip git nginx logrotate curl

echo "=== [2/8] Create app user ==="
id -u $APP_USER &>/dev/null || useradd -m -s /bin/bash $APP_USER

echo "=== [3/8] Clone repository ==="
if [ -d "$APP_DIR" ]; then
  echo "  $APP_DIR already exists, pulling latest..."
  cd $APP_DIR && sudo -u $APP_USER git pull origin main
else
  git clone $REPO_URL $APP_DIR
  chown -R $APP_USER:$APP_USER $APP_DIR
fi

echo "=== [4/8] Create Python virtualenv ==="
sudo -u $APP_USER $PYTHON -m venv $APP_DIR/venv
sudo -u $APP_USER $APP_DIR/venv/bin/pip install --upgrade pip -q
sudo -u $APP_USER $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements-scan.txt -q
# Install live trading deps (only if DRY_RUN=false later)
# sudo -u $APP_USER $APP_DIR/venv/bin/pip install py-clob-client==0.16.0 web3==6.14.0
echo "  Python deps installed in venv"

echo "=== [5/8] Create .env file ==="
if [ ! -f "$APP_DIR/.env" ]; then
  cp $APP_DIR/configs/.env.example $APP_DIR/.env
  chown $APP_USER:$APP_USER $APP_DIR/.env
  chmod 600 $APP_DIR/.env
  echo ""
  echo "  *** ACTION REQUIRED: Edit $APP_DIR/.env and fill in your secrets ***"
  echo "  Run: nano $APP_DIR/.env"
  echo ""
else
  echo "  .env already exists, skipping"
fi

echo "=== [6/8] Create data and log directories ==="
mkdir -p $APP_DIR/data
mkdir -p $LOG_DIR
chown -R $APP_USER:$APP_USER $APP_DIR/data $LOG_DIR

echo "=== [7/8] Install systemd services and cron ==="
cp $APP_DIR/vps/polymarket-scan.service /etc/systemd/system/
cp $APP_DIR/vps/polymarket-resolve.service /etc/systemd/system/
cp $APP_DIR/vps/polymarket-scan.timer /etc/systemd/system/
cp $APP_DIR/vps/polymarket-resolve.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable polymarket-scan.timer polymarket-resolve.timer
systemctl start polymarket-scan.timer polymarket-resolve.timer
echo "  Systemd timers enabled and started"

echo "=== [8/8] Setup nginx for dashboard ==="
cp $APP_DIR/vps/nginx-polymarket.conf /etc/nginx/sites-available/polymarket
ln -sf /etc/nginx/sites-available/polymarket /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "  nginx configured — dashboard available on http://YOUR_VPS_IP/"

echo ""
echo "========================================"
echo "  Setup complete!"
echo "  Next steps:"
echo "  1. Edit secrets:  nano $APP_DIR/.env"
echo "  2. Check status:  systemctl status polymarket-scan.timer"
echo "  3. Watch logs:    journalctl -fu polymarket-scan"
echo "  4. First scan:    systemctl start polymarket-scan.service"
echo "========================================"