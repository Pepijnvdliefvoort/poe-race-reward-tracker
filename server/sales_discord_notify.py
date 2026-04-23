"""Discord webhook notifications for estimated sale signal changes (poller)."""

from __future__ import annotations

from typing import Any

import requests


def _fmt_mirrors(x: Any) -> str | None:
    try:
        v = float(x)
    except Exception:
        return None
    if not (v == v):  # NaN
        return None
    if abs(v) >= 10:
        s = f"{v:.2f}"
    elif abs(v) >= 1:
        s = f"{v:.3f}"
    else:
        s = f"{v:.4f}"
    s = s.rstrip("0").rstrip(".")
    return f"{s} mirrors"


def _fmt_listed_price(amount: Any, currency: Any) -> str | None:
    try:
        if amount is None:
            return None
        v = float(amount)
    except Exception:
        return None
    if not (v == v):  # NaN
        return None
    cur = str(currency or "").strip().lower()
    if not cur:
        return None
    if abs(v) >= 10:
        s = f"{v:.2f}"
    elif abs(v) >= 1:
        s = f"{v:.3f}"
    else:
        s = f"{v:.4f}"
    s = s.rstrip("0").rstrip(".")
    unit = "divines" if cur == "divine" else ("mirrors" if cur == "mirror" else ("exalts" if cur == "exalted" else cur))
    return f"{s} {unit}"


def _event_sentence(ev: dict[str, Any]) -> str | None:
    rule = str(ev.get("rule") or "")

    if rule == "confirmed_transfer":
        seller = str(ev.get("from_seller") or "unknown")
        buyer = str(ev.get("to_seller") or "unknown")
        price = _fmt_listed_price(ev.get("fromPriceAmount"), ev.get("fromPriceCurrency")) or _fmt_mirrors(ev.get("fromMirrorEquiv"))
        if price:
            return f"Seller **{seller}** has sold it for **{price}** to **{buyer}**."
        return f"Seller **{seller}** has sold it to **{buyer}**."

    if rule == "likely_instant_sale":
        seller = str(ev.get("seller") or "unknown")
        price = _fmt_listed_price(ev.get("priceAmount"), ev.get("priceCurrency")) or _fmt_mirrors(ev.get("mirrorEquiv"))
        if price:
            return f"Seller **{seller}** has likely sold it for **{price}** (instant buyout disappeared)."
        return f"Seller **{seller}** has likely sold it (instant buyout disappeared)."

    if rule == "relist_same_seller":
        seller = str(ev.get("seller") or "unknown")
        return f"Seller **{seller}** relisted it (undoes the instant-sale signal)."

    # Intentionally omit inference events that do not contribute to the estimated-sales
    # delta shown in the alert (e.g. reprices, non-instant removals).

    return None


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
    inference_events: list[dict[str, Any]] | None = None,
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

    if inference_events:
        raw_sentences = [s for s in (_event_sentence(ev) for ev in inference_events) if s]
        if raw_sentences:
            counts: dict[str, int] = {}
            for s in raw_sentences:
                counts[s] = counts.get(s, 0) + 1
            sentences = sorted(counts.keys(), key=lambda s: (-counts[s], s))
            lines.append("")
            lines.append("**Signals:**")
            max_lines = 12
            for s in sentences[:max_lines]:
                n = counts.get(s, 1)
                suffix = f" ×{n}" if n > 1 else ""
                lines.append(f"- {s}{suffix}")
            if len(sentences) > max_lines:
                lines.append(f"- …and {len(sentences) - max_lines} more unique signals")

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
    inference_events: list[dict[str, Any]] | None = None,
) -> None:
    embed = build_estimated_sales_embed(
        item_name=item_name,
        item_image_url=item_image_url,
        cycle_delta=cycle_delta,
        total_in_window=total_in_window,
        window_days=window_days,
        confirmed_transfer=confirmed_transfer,
        likely_instant_sale=likely_instant_sale,
        inference_events=inference_events,
    )
    payload: dict[str, Any] = {"embeds": [embed]}
    resp = session.post(webhook_url, json=payload, timeout=10.0)
    resp.raise_for_status()
