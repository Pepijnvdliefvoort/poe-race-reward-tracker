# VPS Deployment Guide

This guide is for hosting the app on a Linux VPS (Ubuntu) with:

- systemd for process management
- Caddy as reverse proxy
- HTTPS via hostname (recommended)

## 1. Prerequisites

- A VPS with public IPv4
- SSH access as root (or a sudo user)
- Repository pushed to GitHub
- Ports 80 and 443 allowed in your VPS/cloud firewall

## 2. Connect To VPS

```bash
ssh root@YOUR_SERVER_IP
```

## 3. Install System Packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip caddy git
```

## 4. Clone Project And Install Python Dependencies

Replace `YOUR_USERNAME/YOUR_REPO` first.

```bash
cd /opt
sudo git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git poe-market-flips
cd /opt/poe-market-flips

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 5. Install And Enable systemd Services

The dashboard unit runs the HTTP server from the repo’s **`server/`** package (entrypoint **`server/server.py`**) with `WorkingDirectory=/opt/poe-market-flips`. Static assets are served from **`web/`** (unchanged). Caddy still reverse-proxies to `127.0.0.1:8080`.

If you previously installed a unit that used **`web/server.py`**, replace it by copying the current unit file again (see [§9](#9-updating-after-new-push) or the snippet below), then `daemon-reload` and restart.

```bash
sudo cp deploy/systemd/poe-market-server.service /etc/systemd/system/
sudo cp deploy/systemd/poe-market-poller.service /etc/systemd/system/

# Optional: set Discord webhook secret for alerts
sudo mkdir -p /etc/poe-market-flips
sudo sh -c 'cat > /etc/poe-market-flips/secrets.env << EOF
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/REPLACE_ME
EOF'
sudo chmod 600 /etc/poe-market-flips/secrets.env

sudo systemctl daemon-reload
sudo systemctl enable --now poe-market-server
sudo systemctl enable --now poe-market-poller
```

## 6. Configure Caddy

Edit Caddy template and set your hostname:

- Best: your own domain like `poe.example.com`
- Good free option: `YOUR_SERVER_IP.sslip.io`

This project’s Caddy config is set up to serve static files (`/assets`, `/css`, `/js`) directly from
`/opt/poe-market-flips/web` (more reliable, and avoids proxying lots of small icon requests through Python).

Then copy config and reload:

```bash
sudo cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 7. Verify Deployment

```bash
sudo systemctl status poe-market-server --no-pager
sudo systemctl status poe-market-poller --no-pager
sudo systemctl status caddy --no-pager
```

```bash
curl -I http://127.0.0.1:8080
curl -I http://127.0.0.1
```

Open in browser:

- `https://YOUR_HOSTNAME`

## 8. Firewall Checklist

If UFW is enabled:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw status
```

Also ensure your cloud firewall (Hetzner or similar) allows inbound TCP 80 and 443.

## 9. Updating After New Push

```bash
cd /opt/poe-market-flips
git pull
.venv/bin/pip install -r requirements.txt
sudo cp deploy/systemd/poe-market-server.service /etc/systemd/system/
sudo cp deploy/systemd/poe-market-poller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart poe-market-server
sudo systemctl restart poe-market-poller
sudo systemctl reload caddy
```

The `cp` lines keep `/etc/systemd/system` in sync when `ExecStart` or other unit fields change (for example after moving the dashboard from `web/server.py` to `server/server.py`, or after changing the poller entry from `poll_item_prices.py` to `python -m poller`).

## 10. Automatic Deploy On Every Push (Recommended)

This repo includes:

- `.github/workflows/deploy-vps.yml` (GitHub Actions workflow)
- `deploy/deploy_on_vps.sh` (commands run on the VPS)

### One-time setup

1. Ensure the app already exists at `/opt/poe-market-flips` on the VPS.
2. In GitHub, open your repository:
	- `Settings -> Secrets and variables -> Actions -> New repository secret`
3. Add these secrets:
	- `VPS_HOST` = your server IP (example: `178.104.113.149`)
	- `VPS_USER` = `root` (or your deploy user)
	- `VPS_PORT` = `22`
	- `VPS_SSH_KEY` = private SSH key content for that VPS user
	- `DISCORD_WEBHOOK_URL` = your Discord webhook URL for price alerts

### Generate an SSH key for deploy (if you do not already have one)

Run on your local machine:

```bash
ssh-keygen -t ed25519 -f vps_deploy_key -C "github-actions-deploy"
```

Add the public key to the VPS user:

```bash
ssh-copy-id -i vps_deploy_key.pub root@YOUR_SERVER_IP
```

Paste the contents of `vps_deploy_key` (private key) into `VPS_SSH_KEY` in GitHub secrets.

### How it works

- On every push to `main` or `master`, GitHub Actions connects to your VPS over SSH.
- It runs `deploy/deploy_on_vps.sh`, which pulls latest code, installs dependencies, writes `/etc/poe-market-flips/secrets.env` from `DISCORD_WEBHOOK_URL` (if provided), restarts services, and reloads Caddy.

`config.json` is intentionally untracked. Keep a server-local copy at `/opt/poe-market-flips/config.json` (for example by copying `config.example.json` once and editing values).

One-time migration on existing VPS hosts (if `config.json` was previously tracked and edited locally):

```bash
cd /opt/poe-market-flips
cp config.json /tmp/poe-config.json.backup
git checkout -- config.json
git pull --ff-only
cp /tmp/poe-config.json.backup config.json
```

Webhook secret notes:

- If `DISCORD_WEBHOOK_URL` is present in GitHub Secrets, it is synced on each deploy.
- If it is missing, deploy keeps any existing `/etc/poe-market-flips/secrets.env` file unchanged.

### First test

In GitHub:

- Open `Actions -> Deploy to VPS -> Run workflow`
- Confirm it succeeds
- Open your site and verify changes are live

## 11. Common Troubleshooting

### Site works in incognito but not normal browser

This is usually cached HTTPS/HSTS state for an old URL. Use your new hostname URL, clear site data, and remove HSTS entry if needed.

### Caddy is running but site is unreachable

Check listeners and local connectivity:

```bash
sudo ss -tulpn | grep ':80'
curl -I http://127.0.0.1:8080
curl -I http://127.0.0.1
```

### Some item icons are missing on VPS but not on Windows

Linux is case-sensitive for filenames. This repo already includes a case-insensitive icon lookup fix in `server/data_service.py`. If icons still look stale, run:

```bash
cd /opt/poe-market-flips
git pull
sudo systemctl restart poe-market-server
```
