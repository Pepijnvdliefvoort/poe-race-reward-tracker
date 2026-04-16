#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/poe-market-flips"

if [ ! -d "$APP_DIR" ]; then
  echo "App directory not found: $APP_DIR"
  exit 1
fi

cd "$APP_DIR"

echo "[1/5] Pull latest code"
git pull --ff-only

echo "[2/5] Install/update Python dependencies"
.venv/bin/pip install -r requirements.txt

echo "[3/5] Sync systemd unit files"
cp deploy/systemd/poe-market-server.service /etc/systemd/system/
cp deploy/systemd/poe-market-poller.service /etc/systemd/system/
systemctl daemon-reload

echo "[4/5] Restart app services"
systemctl restart poe-market-server
systemctl restart poe-market-poller

echo "[5/5] Apply Caddy config"
cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy

systemctl is-active --quiet poe-market-server
systemctl is-active --quiet poe-market-poller
systemctl is-active --quiet caddy

echo "Deployment complete. Services are active."
