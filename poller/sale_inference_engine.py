"""
Heuristic rules for inferring whether trade listings likely sold between polls.

Rules:
1. Same item fingerprint under seller A, then same fingerprint under seller B -> transfer / sold
   (includes sold-then-relisted-by-another seller when both ladders show one seller each).
2. Instant buyout listing gone on next fetch -> likely sold (unless rule 3).
3. Same fingerprint + same seller vanishes then returns next poll -> relist, not a sale (undoes rule 2 or 4b).
4. Non-instant listing gone -> inconclusive if seller appears offline; not counted as sale.
4b. Non-instant listing gone while seller appears online -> likely sold (pending relist can undo).
   The poller prefers a live account-filter search + fetch (`listing.account.online` on another of
   their listings); if that probe fails, it falls back to `sellerOnline` from the prior ladder fetch.
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


def _raw_price_amount_currency(price: dict[str, Any]) -> tuple[float, str] | None:
    """
    Preserve the original listing price (for notifications / UI), even if the currency
    isn't one we can convert to mirror-equivalent.
    """
    amount = price.get("amount")
    currency = price.get("currency")
    if not isinstance(amount, (int, float)) or not isinstance(currency, str):
        return None
    cur = currency.strip().lower()
    if not cur:
        return None
    return float(amount), cur


def _mirror_equivalent(amount: float, currency: str, divines_per_mirror: float) -> float:
    if currency == "mirror":
        return amount
    if currency == "divine":
        return amount / divines_per_mirror
    if currency == "exalted":
        return (amount / EXALTS_PER_DIVINE) / divines_per_mirror
    return float("nan")


def _is_instant_buyout(listing: dict[str, Any], price: dict[str, Any] | None) -> bool:
    # "Instant buyout" (securable) vs in-person listings cannot be reliably inferred from "~b/o"/"~price"
    # alone. In practice, PoE trade returns a `hideout_token` for securable listings; use that signal.
    #
    # This intentionally ignores `price.type` and `note` to avoid misclassifying in-person "~b/o" listings
    # (fixed-price buyouts) as instant.
    return bool(listing.get("hideout_token"))


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


def extract_listing_account_online(entry: dict[str, Any]) -> bool:
    """True if trade fetch shows the listing account as online (PoE ``listing.account.online``)."""
    listing = entry.get("listing") if isinstance(entry, dict) else None
    account = listing.get("account") if isinstance(listing, dict) else None
    if isinstance(account, dict) and "online" in account:
        return bool(account.get("online"))
    return False


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
        seller_online = extract_listing_account_online(entry)
        mirror_eq: float | None = None
        price_amount: float | None = None
        price_currency: str | None = None
        if isinstance(price, dict):
            raw = _raw_price_amount_currency(price)
            if raw is not None:
                amt_raw, cur_raw = raw
                price_amount = round(float(amt_raw), 6)
                price_currency = str(cur_raw)

            # Mirror-equivalent is only available for currencies we normalize.
            norm = _normalize_price_currency(price)
            if norm is not None:
                cur, amt = norm
                m = _mirror_equivalent(float(amt), str(cur), divines_per_mirror)
                if math.isfinite(m):
                    mirror_eq = round(m, 6)
        out.append(
            {
                "fingerprint": fp,
                "seller": seller,
                "isInstant": instant,
                "sellerOnline": seller_online,
                "mirrorEquiv": mirror_eq,
                "priceAmount": price_amount,
                "priceCurrency": price_currency,
            }
        )
    return out


@dataclass
class InferenceCycleResult:
    confirmed_transfer: int = 0
    likely_instant_sale: int = 0
    likely_non_instant_online: int = 0
    relist_same_seller: int = 0
    non_instant_removed: int = 0
    reprice_same_seller: int = 0
    multi_seller_same_fingerprint: int = 0
    new_listing_rows: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_csv_tuple(self) -> tuple[int, int, int, int, int, int, int, int]:
        return (
            self.confirmed_transfer,
            self.likely_instant_sale,
            self.likely_non_instant_online,
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


def safe_to_infer_vanish(
    mirror_equiv: Any,
    *,
    snapshot_truncated: bool = False,
    truncation_cutoff_mirror: float | None = None,
    truncation_safe_margin_pct: float = 6.0,
) -> bool:
    """
    When the API snapshot is truncated (we only see the cheapest N results),
    some previously-seen rows can disappear simply by being pushed past the cutoff.
    """
    if not snapshot_truncated:
        return True
    if truncation_cutoff_mirror is None:
        return False
    m = _as_float(mirror_equiv)
    if m is None:
        return False
    cutoff = float(truncation_cutoff_mirror)
    if cutoff <= 0:
        return False
    margin = max(0.0, float(truncation_safe_margin_pct)) / 100.0
    return m <= cutoff * (1.0 - margin)


def non_instant_vanished_seller_accounts_for_online_probe(
    prev_signals: list[dict[str, Any]],
    curr_signals: list[dict[str, Any]],
    *,
    snapshot_truncated: bool = False,
    truncation_cutoff_mirror: float | None = None,
    truncation_safe_margin_pct: float = 6.0,
) -> list[str]:
    """
    Sellers whose vanished non-instant rows need an online check (same filters as Rule 4/4b).

    Used by the poller to run an account-scoped trade search + fetch before inference.
    """
    prev_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in prev_signals}
    curr_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in curr_signals}
    seen: set[str] = set()
    out: list[str] = []
    for fp, seller in prev_keys - curr_keys:
        if not seller or seller == "unknown":
            continue
        meta = _meta_for(prev_signals, fp, seller)
        if not meta or bool(meta.get("isInstant")):
            continue
        mirror_eq = meta.get("mirrorEquiv")
        if not safe_to_infer_vanish(
            mirror_eq,
            snapshot_truncated=snapshot_truncated,
            truncation_cutoff_mirror=truncation_cutoff_mirror,
            truncation_safe_margin_pct=truncation_safe_margin_pct,
        ):
            continue
        ps = _sellers_for_fingerprint(prev_signals, fp)
        cs = _sellers_for_fingerprint(curr_signals, fp)
        transfer = len(ps) == 1 and len(cs) == 1 and next(iter(ps)) != next(iter(cs))
        if transfer:
            continue
        if seller not in seen:
            seen.add(seller)
            out.append(seller)
    return out


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
    pending_online: list[dict[str, Any]] | None = None,
    seller_online_probe: dict[str, bool] | None = None,
    snapshot_truncated: bool = False,
    truncation_cutoff_mirror: float | None = None,
    truncation_safe_margin_pct: float = 6.0,
) -> tuple[InferenceCycleResult, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Returns (result, new_pending_instant, new_pending_online_non_instant, curr_signals_for_storage).

    Instant buyout rows that vanish (rule 2, not a transfer) credit `likely_instant_sale` on the
    same cycle. A pending entry with `countedImmediate` stays open for one more poll so a same-
    seller relist (rule 3) can decrement that credit. Legacy pendings without `countedImmediate`
    still resolve to +1 on the first later cycle where the row stays gone (old behaviour).

    Non-instant rows that vanish while the seller was online (rule 4b) credit `likely_non_instant_online`;
    pending entries allow a same-seller relist to decrement that credit.

    ``seller_online_probe``: optional map account name -> online bool from a live account search + fetch
    (poller). When present for a seller, overrides ``sellerOnline`` stored on the prior snapshot row.
    """
    result = InferenceCycleResult()
    events: list[dict[str, Any]] = []

    pending_online = pending_online or []
    seller_online_probe = seller_online_probe or {}

    prev_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in prev_signals}
    curr_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in curr_signals}

    # --- Resolve pending non-instant "online" removals (rule 4b vs 3) ---
    new_pending_online: list[dict[str, Any]] = []
    for pend in pending_online:
        fp = str(pend.get("fingerprint") or "")
        seller = str(pend.get("seller") or "")
        removed = int(pend.get("removed_cycle") or 0)
        mirror_eq = pend.get("mirrorEquiv")
        price_amount = pend.get("priceAmount")
        price_currency = pend.get("priceCurrency")
        if not fp or not seller:
            continue
        if (fp, seller) in curr_keys:
            result.relist_same_seller += 1
            result.likely_non_instant_online -= 1
            events.append(
                {
                    "rule": "relist_same_seller",
                    "revertsSaleRule": "likely_non_instant_online_sale",
                    "itemKey": item_key,
                    "fingerprint": fp,
                    "seller": seller,
                    "mirrorEquiv": mirror_eq,
                    "priceAmount": price_amount,
                    "priceCurrency": price_currency,
                    "cycle": cycle,
                }
            )
            continue
        if removed < cycle:
            continue
        new_pending_online.append(pend)

    # --- Resolve older instant removals (rules 2 vs 3) ---
    new_pending_instant: list[dict[str, Any]] = []
    for pend in pending_instant:
        fp = str(pend.get("fingerprint") or "")
        seller = str(pend.get("seller") or "")
        removed = int(pend.get("removed_cycle") or 0)
        mirror_eq = pend.get("mirrorEquiv")
        price_amount = pend.get("priceAmount")
        price_currency = pend.get("priceCurrency")
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
                    "revertsSaleRule": "likely_instant_sale",
                    "itemKey": item_key,
                    "fingerprint": fp,
                    "seller": seller,
                    "mirrorEquiv": mirror_eq,
                    "priceAmount": price_amount,
                    "priceCurrency": price_currency,
                    "cycle": cycle,
                }
            )
            continue
        if removed < cycle:
            if counted_imm:
                pass
            else:
                # Only resolve legacy pending-to-sale when it's safe (not a bump-out).
                if safe_to_infer_vanish(
                    mirror_eq,
                    snapshot_truncated=snapshot_truncated,
                    truncation_cutoff_mirror=truncation_cutoff_mirror,
                    truncation_safe_margin_pct=truncation_safe_margin_pct,
                ):
                    result.likely_instant_sale += 1
                    events.append(
                        {
                            "rule": "likely_instant_sale",
                            "itemKey": item_key,
                            "fingerprint": fp,
                            "seller": seller,
                            "mirrorEquiv": mirror_eq,
                            "priceAmount": price_amount,
                            "priceCurrency": price_currency,
                            "cycle": cycle,
                        }
                    )
            continue
        new_pending_instant.append(pend)

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
                from_meta = _meta_for(prev_signals, fp, a)
                result.confirmed_transfer += 1
                events.append(
                    {
                        "rule": "confirmed_transfer",
                        "itemKey": item_key,
                        "fingerprint": fp,
                        "from_seller": a,
                        "to_seller": b,
                        "fromMirrorEquiv": (from_meta or {}).get("mirrorEquiv"),
                        "fromPriceAmount": (from_meta or {}).get("priceAmount"),
                        "fromPriceCurrency": (from_meta or {}).get("priceCurrency"),
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
        mirror_eq = meta.get("mirrorEquiv")
        price_amount = meta.get("priceAmount")
        price_currency = meta.get("priceCurrency")

        # If we're truncated and this vanished row was near the cutoff, it may have been bumped out.
        if not safe_to_infer_vanish(
            mirror_eq,
            snapshot_truncated=snapshot_truncated,
            truncation_cutoff_mirror=truncation_cutoff_mirror,
            truncation_safe_margin_pct=truncation_safe_margin_pct,
        ):
            continue

        ps = _sellers_for_fingerprint(prev_signals, fp)
        cs = _sellers_for_fingerprint(curr_signals, fp)
        transfer = len(ps) == 1 and len(cs) == 1 and next(iter(ps)) != next(iter(cs))

        if transfer:
            # Listing left A and appeared on B; do not treat A's disappearance as ambiguous instant removal.
            continue

        if not instant:
            if seller in seller_online_probe:
                was_online = bool(seller_online_probe[seller])
            else:
                was_online = bool(meta.get("sellerOnline"))
            if was_online:
                result.likely_non_instant_online += 1
                events.append(
                    {
                        "rule": "likely_non_instant_online_sale",
                        "itemKey": item_key,
                        "fingerprint": fp,
                        "seller": seller,
                        "mirrorEquiv": mirror_eq,
                        "priceAmount": price_amount,
                        "priceCurrency": price_currency,
                        "cycle": cycle,
                    }
                )
                new_pending_online.append(
                    {
                        "fingerprint": fp,
                        "seller": seller,
                        "removed_cycle": cycle,
                        "mirrorEquiv": mirror_eq,
                        "priceAmount": price_amount,
                        "priceCurrency": price_currency,
                    }
                )
            else:
                result.non_instant_removed += 1
                events.append(
                    {
                        "rule": "non_instant_removed_inconclusive",
                        "itemKey": item_key,
                        "fingerprint": fp,
                        "seller": seller,
                        "mirrorEquiv": mirror_eq,
                        "priceAmount": price_amount,
                        "priceCurrency": price_currency,
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
                "mirrorEquiv": mirror_eq,
                "priceAmount": price_amount,
                "priceCurrency": price_currency,
                "cycle": cycle,
            }
        )
        new_pending_instant.append(
            {
                "fingerprint": fp,
                "seller": seller,
                "removed_cycle": cycle,
                "countedImmediate": True,
                "mirrorEquiv": mirror_eq,
                "priceAmount": price_amount,
                "priceCurrency": price_currency,
            }
        )
        events.append(
            {
                "rule": "instant_listing_removed_pending",
                "itemKey": item_key,
                "fingerprint": fp,
                "seller": seller,
                "mirrorEquiv": mirror_eq,
                "priceAmount": price_amount,
                "priceCurrency": price_currency,
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
                    "prevPriceAmount": pm.get("priceAmount"),
                    "prevPriceCurrency": pm.get("priceCurrency"),
                    "currPriceAmount": cm.get("priceAmount"),
                    "currPriceCurrency": cm.get("priceCurrency"),
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
    return result, new_pending_instant, new_pending_online, curr_signals


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
    pending_online = bucket.get("pendingOnlineNonInstant") if isinstance(bucket.get("pendingOnlineNonInstant"), list) else []
    pending_online = [x for x in pending_online if isinstance(x, dict)]

    result, new_pending, new_pending_online, _ = evaluate_listing_transition(
        item_key=item_key,
        cycle=cycle,
        prev_signals=prev_signals,
        curr_signals=curr_signals,
        pending_instant=pending,
        pending_online=pending_online,
        seller_online_probe=None,
        snapshot_truncated=False,
        truncation_cutoff_mirror=None,
    )

    by[item_key] = {
        "signals": curr_signals,
        "pendingInstant": new_pending,
        "pendingOnlineNonInstant": new_pending_online,
        "lastCycle": cycle,
    }
    return result
