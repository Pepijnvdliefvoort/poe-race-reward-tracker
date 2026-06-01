"""Classify new trade listings for the dedicated new-items Discord channel."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from poller.sale_inference_engine import near_floor_for_truncated_instant_vanish


class NewItemsStorage(Protocol):
    def fingerprints_seen_for_variant(
        self, *, variant_id: int, since_cycle: int | None = None, before_cycle: int | None = None
    ) -> set[str]: ...

    def sellers_for_fingerprint_recent(
        self, *, variant_id: int, fingerprint: str, since_cycle: int, before_cycle: int | None = None
    ) -> set[str]: ...

    def last_seen_cycle_for_listing_pair(
        self, *, variant_id: int, fingerprint: str, seller: str, before_cycle: int
    ) -> int | None: ...

    def get_new_item_alert_last_cycle(
        self, *, variant_id: int, fingerprint: str, seller: str
    ) -> int | None: ...


@dataclass(frozen=True)
class NewItemsConfig:
    enabled: bool = True
    known_fp_cycles: int = 5
    cooldown_cycles: int = 5
    return_min_cycles: int = 3
    include_transfers: bool = True
    min_market_listings: int = 1
    jitter_grace_polls: int = 2
    truncated_instant_vanish_max_above_floor_pct: float = 25.0
    truncated_instant_vanish_max_above_floor_mirrors: float = 0.08


@dataclass
class NewItemSignal:
    kind: str
    fingerprint: str
    seller: str
    mirror_equiv: float
    price_amount: float | None
    price_currency: str | None
    is_instant: bool
    from_seller: str | None = None
    other_sellers: list[str] = field(default_factory=list)


def _signal_key(signal: dict[str, Any]) -> tuple[str, str]:
    return (str(signal.get("fingerprint") or ""), str(signal.get("seller") or ""))


def _signal_price(signal: dict[str, Any]) -> float | None:
    value = signal.get("mirrorEquiv")
    if not isinstance(value, (int, float)):
        return None
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def _cheapest_mirror(signals: list[dict[str, Any]]) -> float | None:
    prices = [_signal_price(s) for s in signals]
    valid = [p for p in prices if p is not None]
    return min(valid) if valid else None


def _meta_for(signals: list[dict[str, Any]], fp: str, seller: str) -> dict[str, Any] | None:
    for s in signals:
        if _signal_key(s) == (fp, seller):
            return s
    return None


def _relist_excluded_keys(
    inference_events: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for ev in inference_events:
        if not isinstance(ev, dict):
            continue
        rule = str(ev.get("rule") or "")
        if rule not in {"relist_same_seller", "fetch_jitter_relist"}:
            continue
        fp = str(ev.get("fingerprint") or "").strip()
        seller = str(ev.get("seller") or "").strip()
        if fp and seller:
            out.add((fp, seller))
    return out


def _pending_relist_keys(
    *,
    pending_instant: list[dict[str, Any]],
    pending_online: list[dict[str, Any]],
    curr_keys: set[tuple[str, str]],
    cycle: int,
    jitter_grace_polls: int,
) -> set[tuple[str, str]]:
    """Pairs that vanished recently and reappeared — treat as relist, not new supply."""
    out: set[tuple[str, str]] = set()
    grace = max(0, int(jitter_grace_polls))
    for pend in list(pending_instant) + list(pending_online):
        if not isinstance(pend, dict):
            continue
        fp = str(pend.get("fingerprint") or "").strip()
        seller = str(pend.get("seller") or "").strip()
        if not fp or not seller:
            continue
        key = (fp, seller)
        if key not in curr_keys:
            continue
        removed = int(pend.get("removed_cycle") or 0)
        if removed <= 0:
            continue
        polls_absent = max(0, cycle - removed)
        pend_grace = int(pend.get("jitterGracePolls") or 0)
        max_grace = max(grace, pend_grace)
        if polls_absent <= max_grace:
            out.add(key)
    return out


def _transfer_by_to_seller(
    inference_events: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """Map (fingerprint, to_seller) -> from_seller for confirmed transfers this poll."""
    out: dict[tuple[str, str], str] = {}
    for ev in inference_events:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("rule") or "") != "confirmed_transfer":
            continue
        fp = str(ev.get("fingerprint") or "").strip()
        to_seller = str(ev.get("to_seller") or "").strip()
        from_seller = str(ev.get("from_seller") or "").strip()
        if fp and to_seller and from_seller:
            out[(fp, to_seller)] = from_seller
    return out


def classify_new_items(
    *,
    variant_id: int,
    cycle: int,
    prev_signals: list[dict[str, Any]],
    curr_signals: list[dict[str, Any]],
    inference_events: list[dict[str, Any]],
    pending_instant: list[dict[str, Any]],
    pending_online: list[dict[str, Any]],
    snapshot_truncated: bool,
    storage: NewItemsStorage,
    config: NewItemsConfig,
) -> list[NewItemSignal]:
    if not config.enabled or variant_id <= 0:
        return []

    prev_keys = {_signal_key(s) for s in prev_signals if isinstance(s, dict)}
    curr_keys = {_signal_key(s) for s in curr_signals if isinstance(s, dict)}
    # When the prior poll had no rows, every current row is new vs prev (empty market is not a skip).
    new_keys = curr_keys - prev_keys
    if not new_keys:
        return []

    priced_count = sum(1 for s in curr_signals if _signal_price(s) is not None)
    if priced_count < max(1, int(config.min_market_listings)):
        return []

    relist_keys = _relist_excluded_keys(inference_events)
    pending_keys = _pending_relist_keys(
        pending_instant=pending_instant,
        pending_online=pending_online,
        curr_keys=curr_keys,
        cycle=cycle,
        jitter_grace_polls=config.jitter_grace_polls,
    )
    transfer_map = _transfer_by_to_seller(inference_events)
    floor_mirror = _cheapest_mirror(curr_signals)

    all_fps_ever = storage.fingerprints_seen_for_variant(
        variant_id=variant_id, since_cycle=None, before_cycle=cycle
    )
    since_known = max(0, cycle - max(1, int(config.known_fp_cycles)))
    fps_recent = storage.fingerprints_seen_for_variant(
        variant_id=variant_id, since_cycle=since_known, before_cycle=cycle
    )

    prev_fps = {str(s.get("fingerprint") or "").strip() for s in prev_signals if str(s.get("fingerprint") or "").strip()}
    prev_sellers_by_fp: dict[str, set[str]] = {}
    for s in prev_signals:
        if not isinstance(s, dict):
            continue
        fp = str(s.get("fingerprint") or "").strip()
        seller = str(s.get("seller") or "").strip()
        if fp and seller:
            prev_sellers_by_fp.setdefault(fp, set()).add(seller)

    out: list[NewItemSignal] = []
    for fp, seller in sorted(new_keys):
        if not fp or not seller:
            continue
        if (fp, seller) in relist_keys or (fp, seller) in pending_keys:
            continue

        meta = _meta_for(curr_signals, fp, seller)
        if not meta:
            continue

        price = _signal_price(meta)
        if price is None:
            continue

        if snapshot_truncated and not near_floor_for_truncated_instant_vanish(
            price,
            floor_mirror,
            max_above_floor_pct=config.truncated_instant_vanish_max_above_floor_pct,
            max_above_floor_mirrors=config.truncated_instant_vanish_max_above_floor_mirrors,
        ):
            continue

        last_alert = storage.get_new_item_alert_last_cycle(
            variant_id=variant_id, fingerprint=fp, seller=seller
        )
        if last_alert is not None and cycle - last_alert < max(0, int(config.cooldown_cycles)):
            continue

        from_seller = transfer_map.get((fp, seller))
        if from_seller and config.include_transfers:
            kind = "transfer_listing"
            other_sellers: list[str] = []
        else:
            if from_seller and not config.include_transfers:
                continue

            last_seen = storage.last_seen_cycle_for_listing_pair(
                variant_id=variant_id,
                fingerprint=fp,
                seller=seller,
                before_cycle=cycle,
            )
            if last_seen is not None and cycle - last_seen >= max(1, int(config.return_min_cycles)):
                kind = "returning_after_absence"
                other_sellers = []
            elif fp not in all_fps_ever and fp not in prev_fps:
                kind = "brand_new_roll"
                other_sellers = []
            else:
                recent_sellers = set(prev_sellers_by_fp.get(fp, set()))
                recent_sellers |= storage.sellers_for_fingerprint_recent(
                    variant_id=variant_id,
                    fingerprint=fp,
                    since_cycle=since_known,
                    before_cycle=cycle,
                )
                recent_sellers.discard(seller)
                if fp in fps_recent and recent_sellers:
                    kind = "new_seller_known_roll"
                    other_sellers = sorted(recent_sellers)[:6]
                else:
                    kind = "new_listing_row"
                    other_sellers = sorted(recent_sellers)[:6] if recent_sellers else []

        out.append(
            NewItemSignal(
                kind=kind,
                fingerprint=fp,
                seller=seller,
                mirror_equiv=price,
                price_amount=meta.get("priceAmount") if isinstance(meta.get("priceAmount"), (int, float)) else None,
                price_currency=str(meta.get("priceCurrency") or "") or None,
                is_instant=bool(meta.get("isInstant")),
                from_seller=from_seller,
                other_sellers=other_sellers,
            )
        )

    return out
