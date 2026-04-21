"""
Heuristic rules for inferring whether trade listings likely sold between polls.

Rules:
1. Same item fingerprint under seller A, then same fingerprint under seller B -> transfer / sold.
2. Instant buyout listing gone on next fetch -> likely sold (unless rule 3).
3. Same fingerprint + same seller vanishes then returns next poll -> relist, not a sale.
4. Non-instant listing gone -> inconclusive (seller may be offline); not counted as sale.
5. Same fingerprint + same seller still listed but mirror-equivalent price moved materially -> repriced
   (not a sale; distinguishes relist/reprice from churn).
6. Same fingerprint offered by 2+ different sellers in one fetch -> multi-party contention signal.
7. New (fingerprint, seller) pairs vs previous poll -> fresh supply / new listing rows this cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXALTS_PER_DIVINE = 60.0
# Reprice: relative change >= this, or absolute mirror delta >= REPRICE_ABS_MIRRORS
REPRICE_REL_EPS = 0.02
REPRICE_ABS_MIRRORS = 0.05


def _stack_size_signature(item: dict[str, Any]) -> str:
    """Include stack size when present so two stacks of the same currency differ."""
    props = item.get("properties")
    if not isinstance(props, list):
        return ""
    for p in props:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip().lower()
        if "stack" not in name:
            continue
        vals = p.get("values")
        if isinstance(vals, list) and vals:
            cell = vals[0]
            if isinstance(cell, list) and cell:
                return str(cell[0])
            if isinstance(cell, str):
                return cell
    return ""


def _norm_mod_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = [str(v).strip() for v in raw if isinstance(v, str) and v.strip()]
    out.sort()
    return out


def fingerprint_trade_item(item: dict[str, Any] | None) -> str:
    """Stable hash over mods/flags that identify 'same rolls' for trade comparisons."""
    if not isinstance(item, dict):
        return "no-item"

    parts: list[str] = [
        str(item.get("name") or ""),
        str(item.get("typeLine") or ""),
        str(item.get("baseType") or ""),
        str(item.get("frameType") or ""),
        "|".join(_norm_mod_list(item.get("implicitMods"))),
        "|".join(_norm_mod_list(item.get("explicitMods"))),
        "|".join(_norm_mod_list(item.get("craftedMods"))),
        "|".join(_norm_mod_list(item.get("fracturedMods"))),
        "|".join(_norm_mod_list(item.get("enchantMods"))),
        "|".join(_norm_mod_list(item.get("scourgeMods"))),
        "|".join(_norm_mod_list(item.get("utilityMods"))),
        str(item.get("corrupted") or False),
        str(item.get("mirrored") or False),
        str(item.get("split") or False),
        str(item.get("synthesised") or False),
        str(item.get("veiled") or False),
        str(item.get("identified") if item.get("identified") is not None else ""),
        str(item.get("ilvl") or ""),
        _stack_size_signature(item),
    ]
    sockets = item.get("sockets")
    if isinstance(sockets, list):
        sock_bits = []
        for s in sockets:
            if isinstance(s, dict):
                sock_bits.append(f"{s.get('group')}:{s.get('sColour')}")
        parts.append("|".join(sorted(sock_bits)))

    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _normalize_price_currency(price: dict[str, Any]) -> tuple[str, float] | None:
    amount = price.get("amount")
    currency = price.get("currency")
    if not isinstance(amount, (int, float)) or not isinstance(currency, str):
        return None
    c = currency.strip().lower()
    if c in {"mirror", "mirrors", "mirror of kalandra"}:
        return "mirror", float(amount)
    if c in {"divine", "divines", "div", "divine orb", "divine orbs"}:
        return "divine", float(amount)
    if c in {"exalted", "exalt", "exa", "exalted orb", "exalted orbs"}:
        return "exalted", float(amount)
    return None


def _mirror_equivalent(amount: float, currency: str, divines_per_mirror: float) -> float:
    if currency == "mirror":
        return amount
    if currency == "divine":
        return amount / divines_per_mirror
    if currency == "exalted":
        return (amount / EXALTS_PER_DIVINE) / divines_per_mirror
    return float("nan")


def _is_instant_buyout(listing: dict[str, Any], price: dict[str, Any] | None) -> bool:
    if not isinstance(price, dict):
        return False
    note = str(listing.get("note") or "")
    buyout_type = str(price.get("type") or "")
    return "b/o" in buyout_type.lower() or "~b/o" in note.lower()


def extract_listing_seller_name(entry: dict[str, Any]) -> str:
    listing = entry.get("listing") if isinstance(entry, dict) else None
    account = listing.get("account") if isinstance(listing, dict) else None
    if isinstance(account, dict):
        name = account.get("name")
        if isinstance(name, str) and name:
            return name
        last = account.get("lastCharacterName")
        if isinstance(last, str) and last:
            return last
    return "unknown"


def listing_signals_from_fetch(
    listings: list[dict[str, Any]],
    divines_per_mirror: float,
) -> list[dict[str, Any]]:
    """One row per fetched listing with fields needed for inference + UI."""
    out: list[dict[str, Any]] = []
    for entry in listings:
        if not isinstance(entry, dict):
            continue
        listing = entry.get("listing")
        item = entry.get("item")
        if not isinstance(listing, dict):
            continue
        price = listing.get("price") if isinstance(listing.get("price"), dict) else None
        fp = fingerprint_trade_item(item if isinstance(item, dict) else None)
        seller = extract_listing_seller_name(entry)
        instant = _is_instant_buyout(listing, price)
        mirror_eq: float | None = None
        if isinstance(price, dict):
            norm = _normalize_price_currency(price)
            if norm is not None:
                cur, amt = norm
                m = _mirror_equivalent(amt, cur, divines_per_mirror)
                if math.isfinite(m):
                    mirror_eq = round(m, 6)
        out.append(
            {
                "fingerprint": fp,
                "seller": seller,
                "isInstant": instant,
                "mirrorEquiv": mirror_eq,
            }
        )
    return out


@dataclass
class InferenceCycleResult:
    confirmed_transfer: int = 0
    likely_instant_sale: int = 0
    relist_same_seller: int = 0
    non_instant_removed: int = 0
    reprice_same_seller: int = 0
    multi_seller_same_fingerprint: int = 0
    new_listing_rows: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_csv_tuple(self) -> tuple[int, int, int, int, int, int, int]:
        return (
            self.confirmed_transfer,
            self.likely_instant_sale,
            self.relist_same_seller,
            self.non_instant_removed,
            self.reprice_same_seller,
            self.multi_seller_same_fingerprint,
            self.new_listing_rows,
        )


def _sellers_for_fingerprint(signals: list[dict[str, Any]], fp: str) -> set[str]:
    return {str(s.get("seller") or "") for s in signals if s.get("fingerprint") == fp and str(s.get("seller") or "")}


def _meta_for(
    signals: list[dict[str, Any]],
    fingerprint: str,
    seller: str,
) -> dict[str, Any] | None:
    for s in signals:
        if s.get("fingerprint") == fingerprint and str(s.get("seller") or "") == seller:
            return s
    return None


def _as_float(x: Any) -> float | None:
    if isinstance(x, (int, float)) and math.isfinite(float(x)):
        return float(x)
    return None


def _meaningful_price_change(prev_m: float, curr_m: float) -> bool:
    delta = abs(curr_m - prev_m)
    if delta >= REPRICE_ABS_MIRRORS:
        return True
    if prev_m > 0 and delta / prev_m >= REPRICE_REL_EPS:
        return True
    return False


def _count_multi_seller_fingerprints(signals: list[dict[str, Any]]) -> int:
    """Fingerprints that appear under 2+ distinct sellers in the same snapshot."""
    by_fp: dict[str, set[str]] = {}
    for s in signals:
        fp = str(s.get("fingerprint") or "")
        seller = str(s.get("seller") or "")
        if not fp or not seller:
            continue
        by_fp.setdefault(fp, set()).add(seller)
    return sum(1 for sellers in by_fp.values() if len(sellers) >= 2)


def evaluate_listing_transition(
    *,
    item_key: str,
    cycle: int,
    prev_signals: list[dict[str, Any]],
    curr_signals: list[dict[str, Any]],
    pending_instant: list[dict[str, Any]],
) -> tuple[InferenceCycleResult, list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Returns (result, new_pending_instant, new_prev_signals_for_storage_as_curr).

    Instant buyout rows that vanish (rule 2, not a transfer) credit `likely_instant_sale` on the
    same cycle. A pending entry with `countedImmediate` stays open for one more poll so a same-
    seller relist (rule 3) can decrement that credit. Legacy pendings without `countedImmediate`
    still resolve to +1 on the first later cycle where the row stays gone (old behaviour).
    """
    result = InferenceCycleResult()
    events: list[dict[str, Any]] = []

    prev_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in prev_signals}
    curr_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in curr_signals}

    # --- Resolve older instant removals (rules 2 vs 3) ---
    new_pending: list[dict[str, Any]] = []
    for pend in pending_instant:
        fp = str(pend.get("fingerprint") or "")
        seller = str(pend.get("seller") or "")
        removed = int(pend.get("removed_cycle") or 0)
        if not fp or not seller:
            continue
        counted_imm = bool(pend.get("countedImmediate"))
        if (fp, seller) in curr_keys:
            result.relist_same_seller += 1
            if counted_imm:
                result.likely_instant_sale -= 1
            events.append(
                {
                    "rule": "relist_same_seller",
                    "itemKey": item_key,
                    "fingerprint": fp,
                    "seller": seller,
                    "cycle": cycle,
                }
            )
            continue
        if removed < cycle:
            if counted_imm:
                pass
            else:
                result.likely_instant_sale += 1
                events.append(
                    {
                        "rule": "likely_instant_sale",
                        "itemKey": item_key,
                        "fingerprint": fp,
                        "seller": seller,
                        "cycle": cycle,
                    }
                )
            continue
        new_pending.append(pend)

    # --- Rule 1: seller swap with same fingerprint ---
    all_fps = set()
    for s in prev_signals:
        all_fps.add(str(s.get("fingerprint") or ""))
    for s in curr_signals:
        all_fps.add(str(s.get("fingerprint") or ""))
    all_fps.discard("")

    for fp in all_fps:
        ps = _sellers_for_fingerprint(prev_signals, fp)
        cs = _sellers_for_fingerprint(curr_signals, fp)
        if len(ps) == 1 and len(cs) == 1:
            a = next(iter(ps))
            b = next(iter(cs))
            if a and b and a != b:
                result.confirmed_transfer += 1
                events.append(
                    {
                        "rule": "confirmed_transfer",
                        "itemKey": item_key,
                        "fingerprint": fp,
                        "from_seller": a,
                        "to_seller": b,
                        "cycle": cycle,
                    }
                )

    # --- Vanished keys: new pendings + non-instant (rule 4) ---
    vanished = prev_keys - curr_keys
    for fp, seller in vanished:
        meta = _meta_for(prev_signals, fp, seller)
        if not meta:
            continue
        instant = bool(meta.get("isInstant"))

        ps = _sellers_for_fingerprint(prev_signals, fp)
        cs = _sellers_for_fingerprint(curr_signals, fp)
        transfer = len(ps) == 1 and len(cs) == 1 and next(iter(ps)) != next(iter(cs))

        if transfer:
            # Listing left A and appeared on B; do not treat A's disappearance as ambiguous instant removal.
            continue

        if not instant:
            result.non_instant_removed += 1
            events.append(
                {
                    "rule": "non_instant_removed_inconclusive",
                    "itemKey": item_key,
                    "fingerprint": fp,
                    "seller": seller,
                    "cycle": cycle,
                }
            )
            continue

        result.likely_instant_sale += 1
        events.append(
            {
                "rule": "likely_instant_sale",
                "itemKey": item_key,
                "fingerprint": fp,
                "seller": seller,
                "cycle": cycle,
            }
        )
        new_pending.append(
            {
                "fingerprint": fp,
                "seller": seller,
                "removed_cycle": cycle,
                "countedImmediate": True,
            }
        )
        events.append(
            {
                "rule": "instant_listing_removed_pending",
                "itemKey": item_key,
                "fingerprint": fp,
                "seller": seller,
                "cycle": cycle,
            }
        )

    # --- Rule 5: same listing identity, price moved (reprice / note change) ---
    for fp, seller in prev_keys & curr_keys:
        pm = _meta_for(prev_signals, fp, seller)
        cm = _meta_for(curr_signals, fp, seller)
        if not pm or not cm:
            continue
        a = _as_float(pm.get("mirrorEquiv"))
        b = _as_float(cm.get("mirrorEquiv"))
        if a is None or b is None:
            continue
        if _meaningful_price_change(a, b):
            result.reprice_same_seller += 1
            events.append(
                {
                    "rule": "reprice_same_seller",
                    "itemKey": item_key,
                    "fingerprint": fp,
                    "seller": seller,
                    "prevMirrorEquiv": a,
                    "currMirrorEquiv": b,
                    "cycle": cycle,
                }
            )

    # --- Rule 6: multiple sellers listing the same roll in one ladder slice ---
    result.multi_seller_same_fingerprint = _count_multi_seller_fingerprints(curr_signals)
    if result.multi_seller_same_fingerprint:
        events.append(
            {
                "rule": "multi_seller_same_fingerprint",
                "itemKey": item_key,
                "count": result.multi_seller_same_fingerprint,
                "cycle": cycle,
            }
        )

    # --- Rule 7: brand-new rows vs last poll ---
    result.new_listing_rows = len(curr_keys - prev_keys)
    if result.new_listing_rows and prev_keys:
        events.append(
            {
                "rule": "new_listing_rows",
                "itemKey": item_key,
                "count": result.new_listing_rows,
                "cycle": cycle,
            }
        )

    result.events = events
    return result, new_pending, curr_signals


def load_inference_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "byItemKey": {}}
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return {"version": 1, "byItemKey": {}}
        by = raw.get("byItemKey")
        if not isinstance(by, dict):
            raw["byItemKey"] = {}
        raw.setdefault("version", 1)
        return raw
    except Exception:
        return {"version": 1, "byItemKey": {}}


def save_inference_state(path: Path, root: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(root, fh, ensure_ascii=True, indent=2)
    tmp.replace(path)


def run_inference_for_item(
    *,
    root: dict[str, Any],
    item_key: str,
    cycle: int,
    curr_signals: list[dict[str, Any]],
) -> InferenceCycleResult:
    by = root.setdefault("byItemKey", {})
    if not isinstance(by, dict):
        root["byItemKey"] = {}
        by = root["byItemKey"]

    bucket = by.get(item_key) if isinstance(by.get(item_key), dict) else {}
    prev_signals = bucket.get("signals") if isinstance(bucket.get("signals"), list) else []
    prev_signals = [x for x in prev_signals if isinstance(x, dict)]
    pending = bucket.get("pendingInstant") if isinstance(bucket.get("pendingInstant"), list) else []
    pending = [x for x in pending if isinstance(x, dict)]

    result, new_pending, _ = evaluate_listing_transition(
        item_key=item_key,
        cycle=cycle,
        prev_signals=prev_signals,
        curr_signals=curr_signals,
        pending_instant=pending,
    )

    by[item_key] = {
        "signals": curr_signals,
        "pendingInstant": new_pending,
        "lastCycle": cycle,
    }
    return result
