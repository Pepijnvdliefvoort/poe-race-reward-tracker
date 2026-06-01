[![Deploy to VPS](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/deploy-vps.yml/badge.svg)](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/deploy-vps.yml)

# poe-market-flips

Path of Exile unique-item market tracker with:

- a poller (`python -m poller`) that queries PoE Trade and stores market snapshots
- a web server (`python -m server.server`) that serves the dashboard, compare tools, admin tools, and companion recommendation API
- SQLite as the source of truth (`data/market.db`)

This README reflects the current implementation in this repository.

## What It Does

- Tracks items listed in `items.txt` (including separate art variants via `image_name_filter`).
- Polls the PoE Trade API (Standard league) and stores:
  - poll runs and per-item price summaries
  - listing snapshots used for hover previews and analysis
  - inference events (sale/relist/reprice/new-row signals)
  - inferred sales rows (with late-relist reversal)
- Detects potential flip opportunities and can send Discord alerts.
- Serves a browser UI with charts, filters, compare pages, admin panel, DB explorer, and account compare.
- Provides a companion recommendations endpoint (`/api/companion/recommend`) with heuristic ranking plus optional ML shadow/hybrid scoring.

## Current Architecture

- Poller package: `poller/`
  - entrypoint: `poller/__main__.py`
  - main loop and API/inference logic: `poller/poll_item_prices.py`
  - DB export helper: `poller/db_export.py`
- Server package: `server/`
  - entrypoint: `server/server.py`
  - HTTP routing: `server/http_handler.py`
  - dashboard payload shaping: `server/data_service.py`
  - admin auth/restart/log tools: `server/admin_service.py`
  - recommendations engine: `server/recommendation_service.py`
- Storage layer: `storage/`
  - DB init/migrations: `storage/db.py`, `storage/schema.py`
  - repos and service methods: `storage/repos.py`, `storage/service.py`
- Frontend:
  - pages in `web/` (`index.html`, `admin.html`, `compare.html`, `db.html`)
  - JS modules in `web/js/`
  - CSS modules in `web/css/`
  - icon assets in `web/assets/icons/`

## Data Storage

Primary store:

- `data/market.db`

Schema version in code:

- `SCHEMA_VERSION = 12` (`storage/schema.py`)

Important tables include:

- `items`, `item_variants`
- `poll_runs`, `item_polls`
- `listing_snapshots`
- `inference_events`
- `inference_state_signals`, `inference_state_pending`
- `sales`
- `price_alert_cooldown`
- `app_config`
- visitor/admin support tables (`visits`, `ip_geo_cache`)

Notes:

- `price_poll.csv` is legacy and not the active source of truth.
- `items.txt` and `config.json` are bootstrap inputs; runtime state is persisted in SQLite/app_config.

## Requirements

- Python 3.10+
- Internet access to `www.pathofexile.com`

Python dependencies (`requirements.txt`):

- `requests>=2.31.0`
- `psutil>=5.9.8`

Install:

```bash
pip install -r requirements.txt
```

## Local Environment Variables

Both poller and server load local env files automatically at startup:

- `.env.local`
- `.env`

Load order is `.env.local` then `.env`. Existing process env variables win by default.

Common keys:

```text
DISCORD_WEBHOOK_URL=
DISCORD_WEBHOOK_URL_SALES=
DISCORD_WEBHOOK_URL_REPRICES=
DISCORD_WEBHOOK_URL_NEW_ITEMS=
DISCORD_WEBHOOK_URL_DB_EXPORT=
DISCORD_WEBHOOK_URL_OPS=
DISCORD_WEBHOOK_URL_DAILY_SUMMARY=
ADMIN_TOKEN=
PUBLIC_BASE_URL=
POE_POLLER_RESTART_STRATEGY=
POE_POLLER_SYSTEMD_SERVICE=
POE_POLLER_AUTOSTART=
POE_POLLER_CMD=
POE_VISITORS_INCLUDE_LOCAL=
```

Aliases also supported by code for some webhooks:

- `POE_DISCORD_WEBHOOK_URL`
- `POE_DISCORD_WEBHOOK_URL_DB_EXPORT`
- `POE_DISCORD_WEBHOOK_URL_OPS`
- `POE_DISCORD_WEBHOOK_URL_DAILY_SUMMARY`

## Quick Start (Windows PowerShell)

1. Create and activate venv:

```powershell
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Start poller from repo root:

```powershell
python -m poller
```

3. Start server in another terminal:

```powershell
python -m server.server
```

4. Open:

- `http://127.0.0.1:8080`

## Automated ML Retraining

Run the full retrain pipeline (dataset rebuild -> train candidate models -> backtest -> quality-gated promotion):

```powershell
python .\scripts\retrain_ml_pipeline.py
```

What it does:

- rebuilds `ML/training_30d.csv` from `data/market.db`
- trains candidate artifacts under `ML/candidates/<timestamp>/`
- backtests the candidate models
- compares candidate metrics against current live reports
- promotes candidate models/reports only when gates pass
- archives live artifacts to `ML/archive/<timestamp>/` before promotion

Default quality gates:

- classifier PR-AUC drop <= `0.02`
- classifier ROC-AUC drop <= `0.02`
- regressor MAPE increase <= `0.10`
- backtest `avgRealizedEdgeAllLift` at top-k `20,50,100` remains non-negative and does not drop by more than `0.05`

Useful flags:

```powershell
python .\scripts\retrain_ml_pipeline.py --required-lift-k 20,50,100 --max-lift-drop 0.03
python .\scripts\retrain_ml_pipeline.py --friction 0.25
python .\scripts\retrain_ml_pipeline.py --retain-candidate-days 90 --retain-archive-days 180
python .\scripts\retrain_ml_pipeline.py --force-promote
```

For unattended scheduling on Windows Task Scheduler, use a task action like:

```text
Program/script: C:\Users\pepij\Documents\Repos\poe-market-flips\.venv\Scripts\python.exe
Add arguments: scripts\retrain_ml_pipeline.py
Start in: C:\Users\pepij\Documents\Repos\poe-market-flips
```

### Poller Weekly Trigger

The poller now includes a built-in weekly retrain trigger (similar to daily DB export scheduling).
When enabled, each poll cycle checks whether the scheduled weekly slot has passed and runs:

```text
python scripts/retrain_ml_pipeline.py
```

State is persisted in SQLite config key `ml_retrain`, so it runs at most once per scheduled week.
The retrain process is launched in the background with a lock file (`logs/ml_retrain.lock`) so poll cycles continue while training runs.

### Poller Daily Discord Recap

The poller can post a once-per-day daily recap (charts + stats) to Discord after a configured local time.
Each run covers the **last 24 hours** ending at send time. Point the daily-recap webhook at a **forum** (or media) channel so each run opens a new post and replies inside that thread.

When enabled, each poll cycle checks whether the scheduled daily slot has passed and posts at most once per day.
State is persisted in SQLite config key `daily_summary`.

Environment variables:

- `DISCORD_WEBHOOK_URL_DAILY_SUMMARY` (optional; falls back to `DISCORD_WEBHOOK_URL`)
- `POE_DISCORD_WEBHOOK_URL_DAILY_SUMMARY` (alias)
- `POE_DAILY_SUMMARY_ENABLED` (`1`/`0`, default `1`)
- `POE_DAILY_SUMMARY_HOUR` (`0..23`, default `8`)
- `POE_DAILY_SUMMARY_MINUTE` (`0..59`, default `0`)
- `POE_DAILY_SUMMARY_TZ_OFFSET_MINUTES` (default `120`, GMT+2)
- `POE_DAILY_SUMMARY_TOP_ITEMS` (default `8`, bar chart limit)

Example (`.env.local`):

```text
DISCORD_WEBHOOK_URL_DAILY_SUMMARY=https://discord.com/api/webhooks/...
POE_DAILY_SUMMARY_HOUR=8
POE_DAILY_SUMMARY_MINUTE=15
POE_DAILY_SUMMARY_TZ_OFFSET_MINUTES=120
```

Admin status endpoint:

- `/api/admin/ml-retrain-status`

Environment variables:

- `POE_ML_RETRAIN_WEEKLY_ENABLED` (`1`/`0`, default `1`)
- `POE_ML_RETRAIN_WEEKDAY` (`0..6`, Monday=0, default `6` for Sunday)
- `POE_ML_RETRAIN_HOUR` (`0..23`, default `3`)
- `POE_ML_RETRAIN_MINUTE` (`0..59`, default `30`)
- `POE_ML_RETRAIN_TZ_OFFSET_MINUTES` (default `120`, GMT+2)
- `POE_ML_RETRAIN_TIMEOUT_SECONDS` (default `7200`)
- `POE_ML_RETRAIN_PYTHON` (optional explicit interpreter path)
- `POE_ML_RETRAIN_SCRIPT` (optional script path, default `scripts/retrain_ml_pipeline.py`)

Example (`.env.local`):

```text
POE_ML_RETRAIN_WEEKLY_ENABLED=1
POE_ML_RETRAIN_WEEKDAY=0
POE_ML_RETRAIN_HOUR=4
POE_ML_RETRAIN_MINUTE=15
POE_ML_RETRAIN_TZ_OFFSET_MINUTES=120
POE_ML_RETRAIN_TIMEOUT_SECONDS=10800
```

## Poller CLI

`python -m poller [options]`

Options:

- `--poll-interval <seconds>` (default `3600`; use `0` for back-to-back)
- `--max-cycles <n>` (stop after `n` cycles)
- `--inference-cap <n>` (`0` disables inference fetches; max clamped to PoE search cap)
- `--only <substr...>` (prioritize matched item names first)

Examples:

```powershell
python -m poller --max-cycles 1
python -m poller --poll-interval 1800
python -m poller --inference-cap 50 --max-cycles 1
python -m poller --only Mokou "Demigod's" --max-cycles 1
```

## items.txt Format

Each non-empty, non-comment line is parsed as:

- `Item Name`
- `Item Name|mode`
- `Item Name|mode|category`
- `Item Name|mode|category|image_name_filter`

Where `mode` is one of:

- `aa`
- `normal`
- `any`

`category` is UI metadata.

`image_name_filter` lets you track multiple art variants of the same base item (for example specific icon filename stems).

Examples:

```text
Headhunter|aa|Belt
Demigod's Touch|aa|Gloves|DemigodsTouchAlt.png
Demigod's Touch|aa|Gloves|DemigodsTouch.png
```

## App Config (market)

Runtime config lives under `app_config.key = "market"` in SQLite.

The project can bootstrap this from `config.json` once if no DB config exists.

`config.example.json` contains practical defaults. Current keys include:

- alerting controls:
  - `alert_enabled`
  - `alert_require_flip_signal`
  - `alert_threshold_pct`
  - `alert_history_cycles`
  - `alert_min_total_results`
  - `alert_min_floor_listings`
  - `alert_floor_band_pct`
  - `alert_low_liquidity_extra_drop_pct`
  - `alert_cooldown_cycles`
- inference controls:
  - `inference_listings_fetch_cap`
  - `inference_truncation_safe_margin_pct`
  - `inference_sale_baseline_history_cycles`
  - `inference_sale_unlist_if_above_baseline_pct`
  - `inference_sale_floor_ignore_if_floor_below_mirrors`
  - `inference_sale_baseline_range_mirrors`
  - `late_relist_window_days`
- flip/notify behavior:
  - `notify_flip_min_profit_mirrors_over_1`
  - `notify_flip_min_profit_divines_at_or_below_1`
  - `notify_always_if_cheap_enabled`
  - `notify_always_if_cheap_max_buy_divines`
  - `notify_always_if_cheap_if_next_price_at_least_mirrors`
  - `notify_always_if_buy_currency_exalted`
- other:
  - `sales_discord_window_days`
  - `discord_market_watch_users`
  - `trade_status_option`
  - ops health settings (`ops_health_enabled`, `ops_stale_poll_seconds`, etc.)

## HTTP API (Current)

Public GET routes:

- `/api/prices`
- `/api/config` (GET)
- `/api/listings?queryId=...` (or `variantId`)
- `/api/account-compare`
- `/api/companion/auth`

Companion route:

- `POST /api/companion/recommend`
  - input fields: `wealth`, `currency` (`mirror|divine`), `risk` (`safe|balanced|speculative`), `mode` (`ranked|portfolio`), optional `limit`

Admin route groups (require auth when `ADMIN_TOKEN` is set):

- DB explorer and query:
  - `/api/admin/db/overview`
  - `/api/admin/db/tables`
  - `/api/admin/db/er`
  - `/api/admin/db/table`
  - `/api/admin/db/preview`
  - `POST /api/admin/db/query` (read-only SQL)
- Config/admin operations:
  - `/api/admin/app-config`
  - `/api/admin/app-config/get`
  - `POST /api/admin/app-config/set`
  - `/api/admin/ml-retrain-status`
  - `/api/admin/stats`
  - `/api/admin/logs`
  - `/api/admin/visitor-map`
  - `/api/admin/download/market.db`
  - `POST /api/admin/clear-data`
  - `POST /api/admin/restart-poller`
  - `POST /api/admin/stop-poller`
  - `POST /api/admin/run-db-export`
- Sales/inference admin tools:
  - `/api/admin/market/variants-sales`
  - `/api/admin/market/sales`
  - `POST /api/admin/sales/delete`
  - `POST /api/admin/sales/resend-alert`
  - `POST /api/admin/inference/reset-counters`
  - `POST /api/admin/market/wipe-variant`
  - `POST /api/admin/alerts/test`

## Admin Auth Model

If `ADMIN_TOKEN` is set:

- admin UI and admin API endpoints require auth
- accepted credentials:
  - `?token=...` (can establish a session cookie)
  - `Authorization: Bearer <token>`
  - `admin_session` cookie (HMAC-derived from token)
- lockout protection is enabled for repeated failed credential attempts

If `ADMIN_TOKEN` is not set, admin security is effectively disabled.

## Discord Notifications

Webhook routing:

- main alerts: `DISCORD_WEBHOOK_URL` (or `POE_DISCORD_WEBHOOK_URL`)
- estimated sales: `DISCORD_WEBHOOK_URL_SALES` (fallback to main)
- reprices/new-item watch: `DISCORD_WEBHOOK_URL_REPRICES` (fallback to sales/main)
- all new listings (classified, no pings): `DISCORD_WEBHOOK_URL_NEW_ITEMS` (dedicated channel only; no fallback)
- DB export uploads: `DISCORD_WEBHOOK_URL_DB_EXPORT` (or `POE_DISCORD_WEBHOOK_URL_DB_EXPORT`)
- ops health alerts: `DISCORD_WEBHOOK_URL_OPS` (or `POE_DISCORD_WEBHOOK_URL_OPS`)
- daily recap (charts + stats): `DISCORD_WEBHOOK_URL_DAILY_SUMMARY` (fallback to main)

`discord_market_watch_users` in market config supports mention tagging by seller prefix match.

The daily recap embed includes est. sales, mirrors moved (sales), reprices, top items, and biggest risers/fallers for the rolling 24h window.
For forum (or media) channels, each run creates a new forum post (`thread_name` = `Daily recap · YYYY-MM-DD`); chart PNGs and the stats embed are posted inside that thread. Dashboard-themed charts: top items, reprice activity, and mirrors moved (sales only, cumulative from zero at window start).

## Logging and Runtime Files

- app logs directory: `logs/`
- server log: `logs/server.log`
- poller log: `logs/poller.log`
- poller stdio log (when managed by server subprocess mode): `logs/poller-stdio.log`
- admin lockout state: `logs/admin_auth_lockout.json`
- DB file: `data/market.db`

## VPS Deployment

Main docs:

- `VPS_DEPLOYMENT.md`

Runtime unit files in repo:

- `deploy/systemd/poe-market-server.service`
- `deploy/systemd/poe-market-poller.service`

Current service entrypoints:

- server: `python -m server.server`
- poller: `python -m poller --poll-interval 0`

Deployment helper script:

- `deploy/deploy_on_vps.sh`

It pulls latest code, installs dependencies, syncs secrets, updates systemd units, restarts services, and reloads Caddy.

## Caddy Setup

Template config:

- `deploy/caddy/Caddyfile`

Pattern:

- serve static assets directly from `/opt/poe-market-flips/web`
- reverse proxy dynamic/API traffic to `127.0.0.1:8080`

## ML Ranking Status

ML artifacts are consolidated under `ML/`.

Current baseline and assets:

- `ML/ML_FEATURE_BASELINE.md`
- `ML/build_training_dataset.py`
- `ML/training_30d.csv`
- `ML/training_30d.meta.json`

Runtime recommendations support:

- heuristic ranking (default-safe path)
- ML shadow inference fields in API responses
- hybrid ranking gate (`ml_hybrid_enabled`, alpha blending, confidence-tier gating)

Runtime scoring logic is in `server/recommendation_service.py`.

## Known Limitations / Notes

- Poller currently targets `Standard` league (`DEFAULT_LEAGUE = "Standard"`).
- This tracker is built around unique/equipment market queries and item-name search payloads from PoE Trade.
- Admin DB query endpoint intentionally allows read-only SQL only.

## Development Notes

- Run commands from repository root.
- Server and poller both handle `Ctrl+C` for local shutdown.
- If you edit `items.txt`, startup sync/upsert keeps tracked variants aligned in DB.
