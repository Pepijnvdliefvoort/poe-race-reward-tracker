# poe-market-flips

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

## Notes

- Poll timing is aligned to a fixed start-time grid, not just "sleep N seconds after completion".
- The script includes adaptive rate-limit pacing using response headers.
- Stop either process with `Ctrl+C`.

## Deploy On A VPS (Beginner Friendly)

If you want this online 24/7, run it on a VPS. This project uses two always-on processes:

- `web/server.py` (dashboard + API)
- `poll_item_prices.py` (writes fresh data to `price_poll.csv`)

### Prerequisites

- A Linux VPS (Ubuntu recommended)
- SSH access as `root`
- Your repo already pushed to GitHub

### One-Time Setup Script (run on the VPS)

Replace `YOUR_USERNAME/YOUR_REPO` before running.

```bash
apt update && apt install -y python3 python3-venv python3-pip caddy git

cd /opt
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git poe-market-flips
cd poe-market-flips

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp deploy/systemd/poe-market-server.service /etc/systemd/system/
cp deploy/systemd/poe-market-poller.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now poe-market-server
systemctl enable --now poe-market-poller

cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

### Check That Everything Is Running

```bash
systemctl status poe-market-server --no-pager
systemctl status poe-market-poller --no-pager
systemctl status caddy --no-pager
```

Live logs:

```bash
journalctl -u poe-market-server -f
journalctl -u poe-market-poller -f
```

### Open The Site

If your `deploy/caddy/Caddyfile` is configured with your public IP (for example `178.104.113.149`), open:

- `http://178.104.113.149`

Note: HTTPS certificates are normally issued for domains, not bare IP addresses. For HTTPS, point a domain to the VPS and update `deploy/caddy/Caddyfile` to use that domain.

### Update After You Push New Code

Run this on the VPS:

```bash
cd /opt/poe-market-flips
git pull
.venv/bin/pip install -r requirements.txt
systemctl restart poe-market-server
systemctl restart poe-market-poller
systemctl reload caddy
```

### Common Recovery Commands

Restart services:

```bash
systemctl restart poe-market-server
systemctl restart poe-market-poller
```

Re-read service files after changes:

```bash
systemctl daemon-reload
systemctl restart poe-market-server
systemctl restart poe-market-poller
```
