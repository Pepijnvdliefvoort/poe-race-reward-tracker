# Path of Exile Race Reward Tracker

[![Deploy to VPS](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/deploy-vps.yml/badge.svg)](https://github.com/Pepijnvdliefvoort/poe-race-reward-tracker/actions/workflows/deploy-vps.yml)

Small Path of Exile price polling project for race reward and unique items.

It has 2 parts:
- A poller script that queries the PoE trade API and appends summarized prices to `price_poll.csv`.
- A local dashboard server that reads the CSV and shows live cards/charts in the browser.

## Project Layout

- `poll_item_prices.py`: polling loop and CSV writer
- `items.txt`: tracked items (one per line, with optional mode)
- `price_poll.csv`: generated historical output
- `web/server.py`: server entrypoint
- `web/http_handler.py`: HTTP handler and `/api/prices` route wiring
- `web/data_service.py`: CSV + items parsing, API payload shaping
- `web/index.html`, `web/styles.css`: dashboard shell and styling
- `web/app.js`: dashboard entrypoint/orchestrator
- `web/js/state.js`, `web/js/cards.js`, `web/js/utils.js`: reusable UI modules
- `web/assets/icons/`: local item icons used by the dashboard

## Requirements

- Python 3.10+
- Internet access (for PoE trade API)
- Python package:
  - `requests`

Install dependency:

```bash
pip install requests
```

## Quick Start (Windows PowerShell)

1. Create and activate a virtual environment:

```powershell
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
pip install requests
```

2. Run the poller (writes to `price_poll.csv`):

```powershell
python poll_item_prices.py
```

Optional custom interval (seconds):

```powershell
python poll_item_prices.py --poll-interval 1800
```

3. In another terminal, run the dashboard server:

```powershell
python web/server.py
```

4. Open the dashboard:

- http://127.0.0.1:8080

## items.txt Format

Each non-empty line is one item. Lines starting with `#` are ignored.

Supported formats:

- `Item Name` (default: alternate art only)
- `Item Name|aa` (alternate art only)
- `Item Name|normal` (non-alternate-art only)
- `Item Name|any` (either alt or non-alt)

Examples:

```text
Tabula Rasa
Demigod's Stride|normal
Headhunter|any
```

## Output CSV

`price_poll.csv` is append-only and includes fields such as:

- timestamp and cycle
- item name/mode
- query id
- total/used listings
- mirror and divine low/median/high summaries

If the CSV header does not match the current schema, the poller creates a backup and writes a fresh file with the expected header.

## Alert Noise Control

Alerts compare current cheapest listing vs. a history-based baseline, but low-liquidity markets can produce false positives.
They also require a live resale path: after buying the cheapest listing, the poller checks whether you can relist on your pricing grid and still undercut the next remaining listing.

Current resale grid:

- below or equal to `10` mirrors: relist prices can step in `0.5` mirror increments
- above `10` mirrors: relist prices must leave a full `1` mirror gap

Examples:

- `3.5, 5, 8, 8, 8` alerts because buying at `3.5` can be relisted at `4` or `4.5` and still stay below the next `5` mirror listing
- `5, 5, 8, 8, 8` does not alert because buying one `5` mirror listing still leaves another `5` mirror competitor, so there is no profitable undercut price
- `11, 12, 12, 14, 16` does not alert because the best whole-mirror relist below `12` is `11`, which leaves no profit

You can tune these `config.json` keys to reduce noise:

- `alert_min_total_results` (default `10`): requires enough total market listings before alerts can fire.
- `alert_min_floor_listings` (default `2`): requires at least this many listings near the cheapest price.
- `alert_floor_band_pct` (default `7.5`): defines what "near the floor" means.
- `alert_low_liquidity_extra_drop_pct` (default `20`): extra discount required when total listings are below `alert_min_total_results`.
- `alert_cooldown_cycles` (default `6`): suppresses repeated alerts for essentially the same floor price.

For very thin markets, increase `alert_low_liquidity_extra_drop_pct` to reduce false positives while still allowing alerts for rare 1-2 listing opportunities.

Discord webhook URL is no longer stored in `config.json` or editable in the web UI.
Set it through an environment secret:

- `DISCORD_WEBHOOK_URL` (preferred)
- `POE_DISCORD_WEBHOOK_URL` (fallback alias)

## Notes

- Poll timing is aligned to a fixed start-time grid, not just "sleep N seconds after completion".
- The script includes adaptive rate-limit pacing using response headers.
- Stop either process with `Ctrl+C`.

## Deploy On A VPS

This project runs well on a small VPS with two always-on services:

- `web/server.py` (dashboard + API)
- `poll_item_prices.py` (CSV poller)

Use the full step-by-step deployment guide here:

- [VPS_DEPLOYMENT.md](VPS_DEPLOYMENT.md)

Quick update commands after you push new code:

```bash
cd /opt/poe-market-flips
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart poe-market-server
sudo systemctl restart poe-market-poller
sudo systemctl reload caddy
```

Automatic deploy is also supported via GitHub Actions. See:

- [VPS_DEPLOYMENT.md](VPS_DEPLOYMENT.md#10-automatic-deploy-on-every-push-recommended)
