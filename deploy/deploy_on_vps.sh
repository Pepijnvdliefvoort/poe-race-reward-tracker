#!/usr/bin/env bash
# Deploy script run on the VPS (see .github/workflows/deploy-vps.yml).
# Expects repo at APP_DIR with:
#   - Dashboard: python -m server.server (WorkingDirectory=APP_DIR; static files from web/)
#   - Poller:    python -m poller (package under poller/)
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
git pull --ff-only

echo "[2/6] Install/update Python dependencies"
.venv/bin/pip install -r requirements.txt

echo "[3/6] Sync runtime secrets"
mkdir -p "$SECRETS_DIR"
FINAL_DISCORD="${DISCORD_WEBHOOK_URL:-}"
FINAL_DISCORD_SALES="${DISCORD_WEBHOOK_URL_SALES:-}"
FINAL_DISCORD_DB_EXPORT="${DISCORD_WEBHOOK_URL_DB_EXPORT:-}"
FINAL_ADMIN="${ADMIN_TOKEN:-}"
if [ -f "$SECRETS_FILE" ]; then
  [ -z "$FINAL_DISCORD" ] && FINAL_DISCORD="$(grep '^DISCORD_WEBHOOK_URL=' "$SECRETS_FILE" 2>/dev/null | sed 's/^DISCORD_WEBHOOK_URL=//' | head -1)" || true
  [ -z "$FINAL_DISCORD_SALES" ] && FINAL_DISCORD_SALES="$(grep '^DISCORD_WEBHOOK_URL_SALES=' "$SECRETS_FILE" 2>/dev/null | sed 's/^DISCORD_WEBHOOK_URL_SALES=//' | head -1)" || true
  [ -z "$FINAL_DISCORD_DB_EXPORT" ] && FINAL_DISCORD_DB_EXPORT="$(grep '^DISCORD_WEBHOOK_URL_DB_EXPORT=' "$SECRETS_FILE" 2>/dev/null | sed 's/^DISCORD_WEBHOOK_URL_DB_EXPORT=//' | head -1)" || true
  [ -z "$FINAL_ADMIN" ] && FINAL_ADMIN="$(grep '^ADMIN_TOKEN=' "$SECRETS_FILE" 2>/dev/null | sed 's/^ADMIN_TOKEN=//' | head -1)" || true
fi
if [ -n "${DISCORD_WEBHOOK_URL:-}" ] || [ -n "${DISCORD_WEBHOOK_URL_SALES:-}" ] || [ -n "${DISCORD_WEBHOOK_URL_DB_EXPORT:-}" ] || [ -n "${ADMIN_TOKEN:-}" ] || { [ -n "$FINAL_DISCORD" ] || [ -n "$FINAL_DISCORD_SALES" ] || [ -n "$FINAL_DISCORD_DB_EXPORT" ] || [ -n "$FINAL_ADMIN" ]; }; then
  umask 077
  MERGE_TMP="$(mktemp)"
  {
    [ -n "$FINAL_DISCORD" ] && printf '%s\n' "DISCORD_WEBHOOK_URL=$FINAL_DISCORD"
    [ -n "$FINAL_DISCORD_SALES" ] && printf '%s\n' "DISCORD_WEBHOOK_URL_SALES=$FINAL_DISCORD_SALES"
    [ -n "$FINAL_DISCORD_DB_EXPORT" ] && printf '%s\n' "DISCORD_WEBHOOK_URL_DB_EXPORT=$FINAL_DISCORD_DB_EXPORT"
    [ -n "$FINAL_ADMIN" ] && printf '%s\n' "ADMIN_TOKEN=$FINAL_ADMIN"
  } > "$MERGE_TMP"
  mv "$MERGE_TMP" "$SECRETS_FILE"
  chmod 600 "$SECRETS_FILE"
  echo "Updated $SECRETS_FILE (merge GitHub env with existing values)"
else
  echo "No secrets in workflow or on disk; skipping $SECRETS_FILE"
fi

echo "[4/6] Sync systemd unit files (dashboard: python -m server.server)"
cp deploy/systemd/poe-market-server.service /etc/systemd/system/
cp deploy/systemd/poe-market-poller.service /etc/systemd/system/
systemctl daemon-reload
grep '^ExecStart=' /etc/systemd/system/poe-market-server.service | head -n1 || true

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
