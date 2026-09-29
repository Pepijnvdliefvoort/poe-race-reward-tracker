# poe-market-flips

[![Tests](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/ci.yml?query=branch%3Adevelop)
[![Deploy to VPS](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/deploy-vps.yml/badge.svg)](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/deploy-vps.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.12-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/database-SQLite-003B57?logo=sqlite&logoColor=white)
![Frontend](https://img.shields.io/badge/frontend-vanilla%20JS-F7DF1E?logo=javascript&logoColor=black)
[![Last commit](https://img.shields.io/github/last-commit/Pepijnvdliefvoort/poe-race-reward-tracker/develop)](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/commits/develop)

A market tracker for legacy **Path of Exile** unique items. It polls the official trade site, infers which
listings sold, and serves a live dashboard with price history, Discord alerts and an investment companion
that suggests which items to flip.

## Features

- **Price tracking** for every item in [`items.txt`](items.txt), including separate alt-art variants.
- **Sale inference**: detects sales, relists and reprices from listing changes, reverts a "sale"
  when the item comes back, and ignores fake sales: listings vanishing en masse, one seller pulling
  a stack at once, transfers back and forth between two sellers, and whole spike days.
- **Dashboard** with charts, filters, compare page, alt-art holdings and AA ladder.
- **Investment companion**: ranks items by expected % return per day, with a buy price, a listing plan
  and the chance it sells ([how it works](ML/README.md)).
- **Discord notifications** for flips, sales, reprices, new listings, likely bans, ops health and a
  weekly recap.
- **Admin panel**: config editor, DB explorer, logs, poller restart and ML retrain status.

## How it works

```text
PoE trade API ──► poller ──► SQLite (data/market.db) ──► server ──► dashboard / admin / companion
                    │                                       │
                    └──► Discord alerts                     └──► weekly ML retrain (ML/)
```

| Part | Entry point | What it does |
|---|---|---|
| `poller/` | `python -m poller` | Polls prices, infers sales, sends alerts, runs weekly jobs |
| `server/` | `python -m server.server` | HTTP API, dashboard, admin, companion recommendations |
| `storage/` | | Schema (version 17), migrations, repositories |
| `ML/` | `python scripts/retrain_ml_pipeline.py` | Return-per-day ranking, backtest, gated learned model |
| `web/` | | Plain HTML/CSS/JS pages served by the server |

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; on Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Run the poller and the server in two terminals from the repo root:

```bash
python -m poller
python -m server.server
```

Then open <http://127.0.0.1:8080>. To use the admin page and the companion, set `ADMIN_TOKEN` in
`.env.local` and open `/admin?token=<your token>` once.

## Configuration

| Where | What |
|---|---|
| `items.txt` | Tracked items: `Name`, `Name\|mode`, `Name\|mode\|category` or `Name\|mode\|category\|image_filter` (mode: `aa`, `normal`, `any`) |
| `.env.local` / `.env` | Secrets and runtime switches (loaded automatically; real environment variables win) |
| `app_config` table, key `market` | Alert, inference, flip and companion settings. Seeded once from `config.json` (see [`config.example.json`](config.example.json)), then edited in the admin panel |

Main environment variables:

| Variable | Purpose |
|---|---|
| `ADMIN_TOKEN` | Enables admin and companion auth. Without it, `/admin` is closed |
| `DISCORD_WEBHOOK_URL` | Main alert channel (other channels fall back to it) |
| `DISCORD_WEBHOOK_URL_SALES`, `_REPRICES`, `_NEW_ITEMS`, `_BANS`, `_OPS`, `_DB_EXPORT`, `_WEEKLY_SUMMARY` | Optional per-topic channels |
| `PUBLIC_BASE_URL` | Public site URL, used for links instead of request headers |
| `POE_RATE_LIMIT_RESERVE_RATIO` | Share of the trade API budget left for manual trading (default `0.20`) |
| `POE_ML_RETRAIN_WEEKLY_ENABLED` | Weekly ML retrain in the poller (default on) |

<details>
<summary>All environment variables</summary>

| Variable | Default | Purpose |
|---|---|---|
| `POE_POLLER_RESTART_STRATEGY`, `POE_POLLER_SYSTEMD_SERVICE`, `POE_POLLER_AUTOSTART`, `POE_POLLER_CMD` | | How the admin panel starts and restarts the poller |
| `POE_VISITORS_INCLUDE_LOCAL` | off | Count local visitors in admin stats |
| `POE_PRICES_CHART_MAX_POINTS` | `250` | Downsampling target per variant for `/api/prices` |
| `POE_ML_RETRAIN_WEEKDAY`, `_HOUR`, `_MINUTE`, `_TZ_OFFSET_MINUTES` | Sun 03:30, GMT+2 | Retrain schedule |
| `POE_ML_RETRAIN_TIMEOUT_SECONDS` | `7200` | Retrain time limit |
| `POE_ML_RETRAIN_PYTHON`, `POE_ML_RETRAIN_SCRIPT` | current interpreter, `scripts/retrain_ml_pipeline.py` | Retrain command |
| `POE_WEEKLY_SUMMARY_ENABLED` | on | Weekly Discord recap |
| `POE_WEEKLY_SUMMARY_WEEKDAY`, `_HOUR`, `_MINUTE`, `_TZ_OFFSET_MINUTES` | Sun 12:00, GMT+2 | Recap schedule |
| `POE_WEEKLY_SUMMARY_TOP_ITEMS` | `8` | Items in the recap bar chart |
| `POE_OPS_PROBE_*` | | Settings for the VPS health probe (`python -m server.ops_health_probe`) |

Legacy aliases still work: `POE_DISCORD_WEBHOOK_URL*`, `DISCORD_WEBHOOK_URL_DAILY_SUMMARY` and
`POE_DAILY_SUMMARY_*`.

</details>

<details>
<summary>Poller options</summary>

```bash
python -m poller [--poll-interval SECONDS] [--max-cycles N] [--inference-cap N] [--only SUBSTR ...]
```

- `--poll-interval`: pause between cycles (default `3600`, `0` = back-to-back; the VPS uses `0`)
- `--max-cycles`: stop after N cycles
- `--inference-cap`: listings fetched per item for sale inference (`0` disables)
- `--only`: poll items whose name contains one of these substrings first

</details>

## Investment companion

Open it with the **Invest** button on the dashboard (requires admin auth). Enter a budget and pick a risk
level, and it ranks items by expected % return per day held:

- **Buy** at the cheapest uncorrupted instant-buyout listing.
- **List** in divines just under the cheapest competing copy, or at a whole-mirror price with at most
  two other copies at the same price.
- **Sell chance and days** come from recent sales, the listings ahead of yours, and the separate
  divine and whole-mirror buyers.

The ranking is backtested on recorded history every week. See [`ML/README.md`](ML/README.md) for the
model, data cleaning and backtest results.

## Development

```bash
python -m unittest discover -t . -s . -p "test_*.py"   # full test suite, same as CI
python scripts/retrain_ml_pipeline.py                 # backtest + retrain the ranking
```

- CI runs the syntax check and tests on Python 3.10 and 3.12 for every PR and every push to
  `develop`; pushes to `main` are tested by the deploy workflow before deploying.
- `data/market.db` is the source of truth; treat it as production data.
- See [`CLAUDE.md`](CLAUDE.md) for repository rules (schema migrations, inference and auth safety).

## Deployment

Merging to `main` deploys automatically. The [Deploy to VPS](.github/workflows/deploy-vps.yml)
workflow runs the tests, then SSHes into the VPS and runs `deploy/deploy_on_vps.sh`: pull, install
requirements, sync secrets, restart the systemd services and reload Caddy. The workflow can also be
started manually on any branch.

First-time server setup (systemd units, Caddy, HTTPS) is described in
[`VPS_DEPLOYMENT.md`](VPS_DEPLOYMENT.md).

<details>
<summary>HTTP API</summary>

Public:

| Route | Purpose |
|---|---|
| `GET /api/prices` | Price history + latest poll per item (`?sinceMs=` window or `?full=1`) |
| `GET /api/config` | Dashboard config |
| `GET /api/listings?queryId=` | Current listings for an item |
| `GET /api/account-compare` | Compare seller accounts |
| `GET /api/market/aa-price-points` | AA ladder data |
| `GET /api/companion/auth` | Whether the companion is available to this visitor |
| `POST /api/companion/recommend` | Picks for `wealth`, `currency` (`mirror`/`divine`), `risk` (`safe`/`balanced`/`speculative`), `mode` (`ranked`/`portfolio`) |

Admin (require `ADMIN_TOKEN` via `?token=`, `Authorization: Bearer` or the session cookie; repeated
failures lock out the IP):

- DB explorer: `/api/admin/db/overview`, `tables`, `er`, `table`, `preview`, `POST query` (read-only SQL)
- Config and ops: `/api/admin/app-config` (+ `get`, `POST set`), `stats`, `logs`, `visitor-map`,
  `download/market.db`, `POST clear-data`, `POST restart-poller`, `POST stop-poller`, `POST run-db-export`
- ML: `/api/admin/ml-retrain-status`, `POST trigger-ml-retrain`
- Market tools: `/api/admin/market/variants-sales`, `sales`, `price-points`, `aa-price-points`,
  `POST wipe-variant`, `POST /api/admin/sales/delete`, `POST sales/resend-alert`,
  `POST inference/reset-counters`, `POST alerts/test`

</details>

## Notes

- Tracks the **Standard** league only.
- Built for unique items; currency items are not supported.
- Logs are written to `logs/` (`server.log`, `poller.log`).
