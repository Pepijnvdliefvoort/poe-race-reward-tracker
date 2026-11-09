#!/usr/bin/env bash
# Pull-based deploy for the home server, run by poe-market-update.timer.
# The home server isn't reachable from GitHub, so instead of the Deploy to VPS workflow it checks
# origin/main itself and runs deploy_on_vps.sh (with the home Caddyfile) when main has moved.
set -euo pipefail

APP_DIR="/opt/poe-market-flips"
BRANCH="${POE_DEPLOY_BRANCH:-main}"

cd "$APP_DIR"
git fetch --quiet origin "$BRANCH"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

echo "New commit on $BRANCH: $(git rev-parse --short "$REMOTE") (was $(git rev-parse --short "$LOCAL"))"
# Same marker as the workflow: "[restart-poller]" in the head commit restarts the poller right away.
if git log -1 --format=%B "$REMOTE" | grep -qF "[restart-poller]"; then
  export FORCE_POLLER_RESTART=1
fi
export POE_CADDYFILE="${POE_CADDYFILE:-deploy/caddy/Caddyfile.home}"
exec bash deploy/deploy_on_vps.sh
