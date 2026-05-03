"""Discord webhook notifications for estimated sale signal changes (poller)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests


def _fmt_amount(v: float) -> str:
    if abs(v) >= 10:
        s = f"{v:.2f}"
    elif abs(v) >= 1:
        s = f"{v:.3f}"
    else:
        s = f"{v:.4f}"
    return s.rstrip("0").rstrip(".")


def _fmt_mirrors(x: Any) -> str | None:
    try:
        v = float(x)
    except Exception:
        return None
    if not (v == v):  # NaN
        return None
    return f"{_fmt_amount(v)} mirrors"


def _fmt_mirror_equiv(
    mirror_equiv: Any,
    *,
    divines_per_mirror: float | None,
) -> str | None:
    """
    Prefer divines for sub-mirror prices so the signal reads naturally.
    Fall back to mirrors if the conversion ratio is unavailable.
    """
    try:
        m = float(mirror_equiv)
    except Exception:
        return None
    if not (m == m):  # NaN
        return None
    if divines_per_mirror is None:
        return _fmt_mirrors(m)
    try:
        dpm = float(divines_per_mirror)
    except Exception:
        dpm = float("nan")
    if not (dpm == dpm) or dpm <= 0:  # NaN or invalid
        return _fmt_mirrors(m)

    if 0 <= m < 1:
        d = m * dpm
        return f"{_fmt_amount(d)} divines"
    return f"{_fmt_amount(m)} mirrors"


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


def _fmt_discord_time(utc_iso: Any) -> str | None:
    if not isinstance(utc_iso, str) or not utc_iso.strip():
        return None
    try:
        dt = datetime.fromisoformat(utc_iso.strip())
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = int(dt.timestamp())
    if ts <= 0:
        return None
    return f"<t:{ts}:f> (<t:{ts}:R>)"


def _fmt_short_fp(fp: Any) -> str | None:
    if not isinstance(fp, str):
        return None
    s = fp.strip()
    if not s:
        return None
    return s[:12]


def _event_sentence(ev: dict[str, Any]) -> str | None:
    divines_per_mirror = ev.get("divinesPerMirror")
    rule = str(ev.get("rule") or "")
    fp = _fmt_short_fp(ev.get("fingerprint"))
    fp_suffix = f" FP: `{fp}`" if fp else ""

    if rule == "confirmed_transfer":
        seller = str(ev.get("from_seller") or "unknown")
        buyer = str(ev.get("to_seller") or "unknown")
        price = _fmt_listed_price(ev.get("fromPriceAmount"), ev.get("fromPriceCurrency")) or _fmt_mirror_equiv(
            ev.get("fromMirrorEquiv"),
            divines_per_mirror=divines_per_mirror,
        )
        if price:
            return f"Seller **{seller}** has sold it for **{price}** to **{buyer}**.{fp_suffix}"
        return f"Seller **{seller}** has sold it to **{buyer}**.{fp_suffix}"

    if rule == "likely_instant_sale":
        seller = str(ev.get("seller") or "unknown")
        price = _fmt_listed_price(ev.get("priceAmount"), ev.get("priceCurrency")) or _fmt_mirror_equiv(
            ev.get("mirrorEquiv"),
            divines_per_mirror=divines_per_mirror,
        )
        if price:
            return f"Seller **{seller}** has likely sold it for **{price}** (instant buyout disappeared).{fp_suffix}"
        return f"Seller **{seller}** has likely sold it (instant buyout disappeared).{fp_suffix}"

    if rule == "likely_non_instant_online_sale":
        seller = str(ev.get("seller") or "unknown")
        price = _fmt_listed_price(ev.get("priceAmount"), ev.get("priceCurrency")) or _fmt_mirror_equiv(
            ev.get("mirrorEquiv"),
            divines_per_mirror=divines_per_mirror,
        )
        if price:
            return (
                f"Seller **{seller}** — non-instant listing disappeared while online (likely sold), "
                f"was **{price}**.{fp_suffix}"
            )
        return f"Seller **{seller}** — non-instant listing disappeared while online (likely sold).{fp_suffix}"

    if rule == "relist_same_seller" or rule == "relist_same_seller_late":
        seller = str(ev.get("seller") or "unknown")
        old_price = _fmt_listed_price(ev.get("priceAmount"), ev.get("priceCurrency"))
        new_price = _fmt_listed_price(ev.get("newPriceAmount"), ev.get("newPriceCurrency"))
        fp = str(ev.get("fingerprint") or "")[:12] if ev.get("fingerprint") else None
        reverted_rule = str(ev.get("revertsSaleRule") or "")
        sale_occurred = _fmt_discord_time(ev.get("saleOccurredAtUtc"))
        window_days = ev.get("windowDays")
        
        # Build relist message with more context
        msg_parts = [f"Seller **{seller}** relisted it"]
        if rule == "relist_same_seller_late" and sale_occurred:
            msg_parts.append(f"(reverted sale from {sale_occurred})")
        elif rule == "relist_same_seller_late" and window_days:
            # Fallback for older events without saleOccurredAtUtc metadata.
            msg_parts.append(f"(within {window_days}d relist window)")
        
        if reverted_rule:
            if reverted_rule == "likely_instant_sale":
                msg_parts.append("(undoes instant buyout signal)")
            elif reverted_rule == "likely_non_instant_online_sale":
                msg_parts.append("(undoes non-instant online signal)")
        else:
            msg_parts.append("(undoes a prior signal)")
        
        msg = " ".join(msg_parts)
        
        # Add price and fingerprint info
        details = []
        if old_price and new_price and old_price != new_price:
            details.append(f"Price: **{old_price}** → **{new_price}**")
        elif old_price or new_price:
            price = new_price or old_price
            details.append(f"Price: **{price}**")
        
        if fp:
            details.append(f"FP: `{fp}`")
        
        if details:
            msg += " — " + " | ".join(details)
        
        return msg + "."

    return None


def _estimated_sales_rules_breakdown(
    *,
    confirmed_transfer: int,
    likely_instant_sale: int,
    likely_non_instant_online: int,
) -> str:
    """Human-readable mapping to ``sale_inference_engine`` rule counters."""
    parts: list[str] = []
    if confirmed_transfer:
        parts.append(f"Rule 1 (transfer / sold): **{confirmed_transfer:+d}**")
    if likely_instant_sale > 0:
        parts.append(f"Rule 2 (instant B/O gone, likely sold): **{likely_instant_sale:+d}**")
    elif likely_instant_sale < 0:
        parts.append(f"Rule 3 (relist — undid instant / online-sale signal): **{likely_instant_sale:+d}**")
    if likely_non_instant_online:
        parts.append(f"Rule 4b (non-instant gone, seller was online): **{likely_non_instant_online:+d}**")
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
    likely_non_instant_online: int = 0,
    divines_per_mirror: float | None = None,
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
        likely_non_instant_online=likely_non_instant_online,
    )
    if rules:
        lines.append(rules)

    if inference_events:
        enriched_events: list[dict[str, Any]] = []
        for ev in inference_events:
            if isinstance(ev, dict):
                ev2 = dict(ev)
                if divines_per_mirror is not None:
                    ev2.setdefault("divinesPerMirror", divines_per_mirror)
                enriched_events.append(ev2)
        raw_sentences = [s for s in (_event_sentence(ev) for ev in enriched_events) if s]
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
    likely_non_instant_online: int = 0,
    divines_per_mirror: float | None = None,
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
        likely_non_instant_online=likely_non_instant_online,
        divines_per_mirror=divines_per_mirror,
        inference_events=inference_events,
    )
    payload: dict[str, Any] = {"embeds": [embed]}
    resp = session.post(webhook_url, json=payload, timeout=10.0)
    resp.raise_for_status()
