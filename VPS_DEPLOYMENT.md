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

```bash
sudo cp deploy/systemd/poe-market-server.service /etc/systemd/system/
sudo cp deploy/systemd/poe-market-poller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now poe-market-server
sudo systemctl enable --now poe-market-poller
```

## 6. Configure Caddy

Edit Caddy template and set your hostname:

- Best: your own domain like `poe.example.com`
- Good free option: `YOUR_SERVER_IP.sslip.io`

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
sudo systemctl restart poe-market-server
sudo systemctl restart poe-market-poller
sudo systemctl reload caddy
```

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
- It runs `deploy/deploy_on_vps.sh`, which pulls latest code, installs dependencies, restarts services, and reloads Caddy.

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

Linux is case-sensitive for filenames. This repo already includes a case-insensitive icon lookup fix in `web/data_service.py`. If icons still look stale, run:

```bash
cd /opt/poe-market-flips
git pull
sudo systemctl restart poe-market-server
```
