"""Discord webhook notifications for estimated sale signal changes (poller)."""

from __future__ import annotations

from typing import Any

import requests


def _estimated_sales_rules_breakdown(
    *,
    confirmed_transfer: int,
    likely_instant_sale: int,
) -> str:
    """Human-readable mapping to ``sale_inference_engine`` doc rules 1–3."""
    parts: list[str] = []
    if confirmed_transfer:
        parts.append(f"Rule 1 (transfer / sold): **{confirmed_transfer:+d}**")
    if likely_instant_sale > 0:
        parts.append(f"Rule 2 (instant B/O gone, likely sold): **{likely_instant_sale:+d}**")
    elif likely_instant_sale < 0:
        parts.append(f"Rule 3 (relist — undid Rule 2): **{likely_instant_sale:+d}**")
    return " | ".join(parts)


def build_estimated_sales_embed(
    *,
    item_name: str,
    item_image_url: str | None,
    cycle_delta: int,
    total_in_window: int,
    window_days: int,
    confirmed_transfer: int,
    likely_instant_sale: int,
) -> dict[str, Any]:
    label = f"last {window_days} day" + ("" if window_days == 1 else "s")
    sign = "+" if cycle_delta > 0 else ""
    lines = [
        f"**This poll:** {sign}{cycle_delta}",
    ]
    rules = _estimated_sales_rules_breakdown(
        confirmed_transfer=confirmed_transfer,
        likely_instant_sale=likely_instant_sale,
    )
    if rules:
        lines.append(rules)
    lines.append(f"**Total est. sold ({label}):** ~{total_in_window}")
    embed: dict[str, Any] = {
        "title": f"Est. sales: {item_name}",
        "description": "\n".join(lines),
        "color": 0x2ECC71 if cycle_delta > 0 else (0xE67E22 if cycle_delta < 0 else 0x95A5A6),
    }
    if item_image_url:
        embed["thumbnail"] = {"url": item_image_url}
    return embed


def send_estimated_sales_change_notification(
    session: requests.Session,
    *,
    webhook_url: str,
    item_name: str,
    item_image_url: str | None,
    cycle_delta: int,
    total_in_window: int,
    window_days: int,
    confirmed_transfer: int,
    likely_instant_sale: int,
) -> None:
    embed = build_estimated_sales_embed(
        item_name=item_name,
        item_image_url=item_image_url,
        cycle_delta=cycle_delta,
        total_in_window=total_in_window,
        window_days=window_days,
        confirmed_transfer=confirmed_transfer,
        likely_instant_sale=likely_instant_sale,
    )
    payload: dict[str, Any] = {"content": "@here", "embeds": [embed]}
    resp = session.post(webhook_url, json=payload, timeout=10.0)
    resp.raise_for_status()
