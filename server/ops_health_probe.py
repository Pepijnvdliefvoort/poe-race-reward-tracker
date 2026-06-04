#!/usr/bin/env python3
"""VPS cron probe: local HTTP checks + Discord ops webhook on failure/slow responses."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests

from env_loader import load_local_env

load_local_env()

from server.ops_discord import load_discord_ops_webhook_url_from_env, send_ops_alert

ROOT_DIR = Path(__file__).resolve().parents[1]
SECRETS_PATH = Path("/etc/poe-market-flips/secrets.env")
STATE_PATH = Path("/var/lib/poe-market-flips/ops_probe_state.json")
BASE_URL = os.getenv("POE_OPS_PROBE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
CONFIG_TIMEOUT_S = float(os.getenv("POE_OPS_PROBE_CONFIG_TIMEOUT", "8"))
PRICES_TIMEOUT_S = float(os.getenv("POE_OPS_PROBE_PRICES_TIMEOUT", "45"))
PRICES_MAX_SECONDS = float(os.getenv("POE_OPS_PROBE_PRICES_MAX_SEC", "30"))
PRICES_MAX_BYTES = int(os.getenv("POE_OPS_PROBE_PRICES_MAX_BYTES", str(25 * 1024 * 1024)))
COOLDOWN_SECONDS = int(os.getenv("POE_OPS_PROBE_COOLDOWN_SEC", "1800"))
WINDOW_DAYS = int(os.getenv("POE_OPS_PROBE_WINDOW_DAYS", "90"))


def _load_system_secrets() -> None:
    if not SECRETS_PATH.is_file():
        return
    for raw_line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _load_state() -> dict[str, float]:
    try:
        if STATE_PATH.is_file():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, float]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _probe_get(path: str, *, timeout_s: float) -> tuple[int, int, float, str | None]:
    url = f"{BASE_URL}{path}"
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read()
            elapsed = time.monotonic() - started
            return int(resp.status), len(body), elapsed, None
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        err_body = exc.read() if exc.fp else b""
        return int(exc.code), len(err_body), elapsed, str(exc)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        return 0, 0, elapsed, str(exc)


def _should_alert(state: dict[str, float], key: str) -> bool:
    now = time.monotonic()
    last = float(state.get(key, 0.0))
    if last > 0.0 and (now - last) < float(COOLDOWN_SECONDS):
        return False
    state[key] = now
    return True


def _notify(
    session: requests.Session,
    webhook: str,
    state: dict[str, float],
    *,
    key: str,
    title: str,
    details: str,
    severity: str,
) -> None:
    if not _should_alert(state, key):
        return
    try:
        send_ops_alert(session, webhook, title=title, details=details, severity=severity)
    except Exception as exc:  # noqa: BLE001
        print(f"[ops_probe] discord failed ({key}): {exc}", file=sys.stderr)


def main() -> int:
    _load_system_secrets()
    webhook = load_discord_ops_webhook_url_from_env()
    if not webhook:
        print("[ops_probe] DISCORD_WEBHOOK_URL_OPS not set; skipping")
        return 0

    state = _load_state()
    session = requests.Session()
    issues = 0

    status, size, elapsed, err = _probe_get("/api/config", timeout_s=CONFIG_TIMEOUT_S)
    if err or status != 200:
        issues += 1
        _notify(
            session,
            webhook,
            state,
            key="config-down",
            title="Dashboard /api/config unhealthy",
            details=f"GET {BASE_URL}/api/config → status={status} error={err or 'n/a'} ({elapsed:.1f}s)",
            severity="critical",
        )
    elif elapsed > CONFIG_TIMEOUT_S:
        issues += 1
        _notify(
            session,
            webhook,
            state,
            key="config-slow",
            title="Dashboard /api/config slow",
            details=f"GET /api/config took {elapsed:.1f}s (limit {CONFIG_TIMEOUT_S:.0f}s)",
            severity="warning",
        )

    since_ms = int((datetime.now(timezone.utc).timestamp() - WINDOW_DAYS * 86400) * 1000)
    prices_path = f"/api/prices?sinceMs={since_ms}"
    status, size, elapsed, err = _probe_get(prices_path, timeout_s=PRICES_TIMEOUT_S)
    if err or status != 200:
        issues += 1
        _notify(
            session,
            webhook,
            state,
            key="prices-down",
            title="Dashboard /api/prices unhealthy",
            details=(
                f"GET {BASE_URL}{prices_path} → status={status} "
                f"error={err or 'n/a'} ({elapsed:.1f}s)"
            ),
            severity="critical",
        )
    else:
        if elapsed > PRICES_MAX_SECONDS:
            issues += 1
            _notify(
                session,
                webhook,
                state,
                key="prices-slow",
                title="Dashboard /api/prices slow",
                details=(
                    f"Windowed prices took {elapsed:.1f}s (limit {PRICES_MAX_SECONDS:.0f}s), "
                    f"size={size / (1024 * 1024):.1f} MiB"
                ),
                severity="warning",
            )
        if size > PRICES_MAX_BYTES:
            issues += 1
            _notify(
                session,
                webhook,
                state,
                key="prices-huge",
                title="Dashboard /api/prices payload too large",
                details=(
                    f"Windowed ({WINDOW_DAYS}d) response is {size / (1024 * 1024):.1f} MiB "
                    f"(limit {PRICES_MAX_BYTES / (1024 * 1024):.0f} MiB)"
                ),
                severity="warning",
            )

    _save_state(state)
    if issues:
        print(f"[ops_probe] {issues} issue(s) detected")
        return 1
    print("[ops_probe] ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
