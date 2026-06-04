from __future__ import annotations

import os

import requests

OPS_DISCORD_ROLE_ID = "1503672022163525744"


def load_discord_ops_webhook_url_from_env() -> str:
    for env_name in ("DISCORD_WEBHOOK_URL_OPS", "POE_DISCORD_WEBHOOK_URL_OPS"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


def send_ops_alert(
    session: requests.Session,
    webhook_url: str,
    *,
    title: str,
    details: str,
    severity: str,
) -> None:
    if not webhook_url:
        return
    level = (severity or "warning").strip().lower()
    color = 0x3498DB
    if level == "warning":
        color = 0xF39C12
    elif level == "critical":
        color = 0xE74C3C
    embed = {
        "title": f"OPS: {title}",
        "description": details,
        "color": color,
    }
    payload = {
        "content": f"<@&{OPS_DISCORD_ROLE_ID}>",
        "allowed_mentions": {"parse": [], "roles": [OPS_DISCORD_ROLE_ID]},
        "embeds": [embed],
    }
    response = session.post(webhook_url, json=payload, timeout=10.0)
    response.raise_for_status()
