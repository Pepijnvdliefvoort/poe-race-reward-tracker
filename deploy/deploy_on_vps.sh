#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/poe-market-flips"
SECRETS_DIR="/etc/poe-market-flips"
SECRETS_FILE="$SECRETS_DIR/secrets.env"

if [ ! -d "$APP_DIR" ]; then
  echo "App directory not found: $APP_DIR"
  exit 1
fi

cd "$APP_DIR"

echo "[1/6] Pull latest code"
# GitHub is the source of truth for config.json during deploy.
git reset --quiet HEAD -- "config.json" || true
git checkout -- "config.json" || true
echo "Reset local config.json to repository version"

git pull --ff-only

echo "[2/6] Install/update Python dependencies"
.venv/bin/pip install -r requirements.txt

echo "[3/6] Sync runtime secrets"
mkdir -p "$SECRETS_DIR"
if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
  umask 077
  cat > "$SECRETS_FILE" <<EOF
DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL}
EOF
  chmod 600 "$SECRETS_FILE"
  echo "Updated $SECRETS_FILE from GitHub secret DISCORD_WEBHOOK_URL"
else
  if [ -f "$SECRETS_FILE" ]; then
    echo "DISCORD_WEBHOOK_URL not provided by workflow; keeping existing $SECRETS_FILE"
  else
    echo "DISCORD_WEBHOOK_URL not provided and no existing $SECRETS_FILE; alerts will remain disabled"
  fi
fi

echo "[4/6] Sync systemd unit files"
cp deploy/systemd/poe-market-server.service /etc/systemd/system/
cp deploy/systemd/poe-market-poller.service /etc/systemd/system/
systemctl daemon-reload

echo "[5/6] Restart app services"
systemctl restart poe-market-server
systemctl restart poe-market-poller

echo "[6/6] Apply Caddy config"
if grep -q "PUBLIC_HOSTNAME_HERE" deploy/caddy/Caddyfile; then
  echo "Refusing deploy: deploy/caddy/Caddyfile still contains PUBLIC_HOSTNAME_HERE"
  exit 1
fi
cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy

systemctl is-active --quiet poe-market-server
systemctl is-active --quiet poe-market-poller
systemctl is-active --quiet caddy

echo "Deployment complete. Services are active."
