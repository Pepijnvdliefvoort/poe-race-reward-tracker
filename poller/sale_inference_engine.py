"""
Heuristic rules for inferring whether trade listings likely sold between polls.

Rules:
1. Same item fingerprint under seller A, then same fingerprint under seller B -> transfer / sold
   (includes sold-then-relisted-by-another seller when both ladders show one seller each).
2. Instant buyout listing gone on next fetch -> likely sold (unless rule 3).
2a. When the trade search has more results than we fetch, instant vanishes only count if the
    listing was near the floor band (buyers take cheap stock; mid-ladder rows often drop from
    the fetched ID slice between polls). Deferred counting applies in that case (rule 2b).
2b. Truncated-snapshot instant vanishes defer the sale signal for a grace window so a quick
    reappearance is treated as fetch jitter, not a sale + relist alert pair.
3. Same fingerprint + same seller vanishes then returns next poll -> relist, not a sale (undoes rule 2 or 4b).
4. Non-instant listing gone -> inconclusive if seller appears offline; not counted as sale.
4b. Non-instant listing gone while seller appears online -> likely sold (pending relist can undo).
   The poller prefers a live account-filter search + fetch (`listing.account.online` on another of
   their listings); if that probe fails, it falls back to `sellerOnline` from the prior ladder fetch.
   PoE's online flag can lag a real logout, so this credit defers for a short grace window (see
   `non_instant_online_grace_polls`) before counting, to avoid alerting on a seller who just logged off.
5. Same fingerprint + same seller still listed but listed price changed -> repriced
   (not a sale; only when that pair maps to exactly one listing on both polls).
6. Same fingerprint offered by 2+ different sellers in one fetch -> multi-party contention signal.
7. New (fingerprint, seller) pairs vs previous poll -> fresh supply / new listing rows this cycle.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

EXALTS_PER_DIVINE = 60.0

# Bump when `fingerprint_trade_item` inputs change. On poller startup a stored
# version mismatch clears inference_state_* so the first post-deploy poll does
# not treat every listing as vanished (false sales).
FINGERPRINT_VERSION = 1


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


def _is_instant_buyout(
    listing: dict[str, Any],
    price: dict[str, Any] | None,
    *,
    allow_fixed_price_fallback: bool,
) -> bool:
    """
    Classify a listing as instant vs non-instant.

    Intentional rule:
    - If `listing.fee` is present (and > 0) => instant
    - Otherwise => non-instant

    Notes:
    - `price` and `allow_fixed_price_fallback` are accepted for backward compatibility with older callers,
      but are intentionally ignored by this rule.
    """
    fee = listing.get("fee")
    return isinstance(fee, (int, float)) and float(fee) > 0


def is_instant_buyout_listing(
    listing: dict[str, Any],
    price: dict[str, Any] | None,
    *,
    allow_fixed_price_fallback: bool,
) -> bool:
    """Public wrapper so other modules can share the same classifier."""
    return _is_instant_buyout(
        listing,
        price,
        allow_fixed_price_fallback=allow_fixed_price_fallback,
    )


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
        instant = is_instant_buyout_listing(
            listing,
            price,
            allow_fixed_price_fallback=False,
        )
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


def _signal_pair_counts(signals: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for s in signals:
        fp = str(s.get("fingerprint") or "")
        seller = str(s.get("seller") or "")
        if not fp or not seller:
            continue
        key = (fp, seller)
        count_hint = s.get("signalCount")
        if isinstance(count_hint, int) and count_hint > 0:
            counts[key] = max(counts.get(key, 0), int(count_hint))
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _as_float(x: Any) -> float | None:
    if isinstance(x, (int, float)) and math.isfinite(float(x)):
        return float(x)
    return None


def _priced_too_high_vs_baseline(
    mirror_equiv: Any,
    *,
    baseline_mirror: float | None,
    max_above_baseline_pct: float,
) -> bool:
    """
    Guardrail: a vanished listing priced far above baseline is more likely an unlisting
    than a "someone bought it", because buyers usually take the cheapest listing.
    """
    if baseline_mirror is None:
        return False
    base = float(baseline_mirror)
    if not math.isfinite(base) or base <= 0:
        return False
    m = _as_float(mirror_equiv)
    if m is None or m <= 0:
        return False
    pct = float(max_above_baseline_pct)
    if not math.isfinite(pct):
        pct = 0.0
    pct = max(0.0, pct)
    return m > base * (1.0 + (pct / 100.0))


def _is_low_floor_market(
    cheapest_mirror: float | None,
    *,
    floor_below_mirrors: float,
) -> bool:
    if cheapest_mirror is None:
        return False
    floor = float(cheapest_mirror)
    if not math.isfinite(floor) or floor <= 0:
        return False
    floor_cap = float(floor_below_mirrors)
    if not math.isfinite(floor_cap) or floor_cap <= 0:
        return False
    return floor < floor_cap


def _cheapest_mirror_equiv(signals: list[dict[str, Any]]) -> float | None:
    """Return the current cheapest valid mirror-equivalent listing in a snapshot."""
    vals = [
        float(s.get("mirrorEquiv"))
        for s in signals
        if isinstance(s, dict)
        and isinstance(s.get("mirrorEquiv"), (int, float))
        and math.isfinite(float(s.get("mirrorEquiv")))
        and float(s.get("mirrorEquiv")) > 0
    ]
    return min(vals) if vals else None


def _priced_outside_baseline_range_sub10(
    mirror_equiv: Any,
    *,
    cheapest_mirror: float | None,
    baseline_mirror: float | None,
    floor_below_mirrors: float,
    baseline_range_mirrors: float,
) -> bool:
    """
    Guardrail: in low-price markets, ignore inferred sales far above the floor when they
    are also above the baseline (more likely unlist/reprice than a true sale).

    Vanishes at or below the baseline are always sale-like. Floor-priced rows count too
    even when the baseline median is much higher.
    """
    if cheapest_mirror is None:
        return False
    floor = float(cheapest_mirror)
    if not math.isfinite(floor) or floor <= 0:
        return False

    floor_cap = float(floor_below_mirrors)
    range_delta = float(baseline_range_mirrors)
    if not math.isfinite(floor_cap) or floor_cap <= 0:
        return False
    if not math.isfinite(range_delta) or range_delta <= 0:
        return False

    if floor >= floor_cap:
        return False

    if baseline_mirror is None:
        return False
    base = float(baseline_mirror)
    if not math.isfinite(base) or base <= 0:
        return False

    m = _as_float(mirror_equiv)
    if m is None or m <= 0:
        return False

    # At or below baseline: always sale-like (buyers take cheap listings).
    if m <= base:
        return False

    # Above baseline but still at/near the floor band (e.g. 4m when floor is 4m).
    if m <= floor + range_delta:
        return False

    # Far above the floor and above baseline → likely an unlist, not a sale.
    return m > floor + range_delta


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


def near_floor_for_truncated_instant_vanish(
    mirror_equiv: Any,
    cheapest_mirror: float | None,
    *,
    max_above_floor_pct: float = 25.0,
    max_above_floor_mirrors: float = 0.08,
) -> bool:
    """
    When total_results exceeds the inference fetch cap, a row can vanish because its search ID
    fell out of the fetched slice — not because it sold. Only treat instant vanishes as sale-like
    when the listing was priced at/near the current floor (where buyers actually trade).
    """
    if cheapest_mirror is None:
        return True
    floor = float(cheapest_mirror)
    if not math.isfinite(floor) or floor <= 0:
        return True
    m = _as_float(mirror_equiv)
    if m is None or m <= 0:
        return False
    pct = max(0.0, float(max_above_floor_pct))
    abs_band = max(0.0, float(max_above_floor_mirrors))
    band = max(floor * (pct / 100.0), abs_band)
    return m <= floor + band


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


@dataclass(frozen=True)
class VanishedListing:
    fingerprint: str
    seller: str
    mirror_equiv: float | None
    is_instant: bool


def collect_inference_safe_vanishes(
    prev_signals: list[dict[str, Any]],
    curr_signals: list[dict[str, Any]],
    *,
    snapshot_truncated: bool = False,
    truncation_cutoff_mirror: float | None = None,
    truncation_safe_margin_pct: float = 6.0,
    truncated_instant_vanish_max_above_floor_pct: float = 25.0,
    truncated_instant_vanish_max_above_floor_mirrors: float = 0.08,
) -> list[VanishedListing]:
    """
  Listings that disappeared between polls and pass the same vanish guards as the inference engine.

  Includes instant and non-instant rows (sales and unlist-style removals) for account-ban detection.
  """
    prev_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in prev_signals}
    curr_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in curr_signals}
    cheapest_prev_mirror = _cheapest_mirror_equiv(prev_signals)
    out: list[VanishedListing] = []
    for fp, seller in prev_keys - curr_keys:
        if not seller or seller == "unknown":
            continue
        meta = _meta_for(prev_signals, fp, seller)
        if not meta:
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
        instant = bool(meta.get("isInstant"))
        if instant and snapshot_truncated and not near_floor_for_truncated_instant_vanish(
            mirror_eq,
            cheapest_prev_mirror,
            max_above_floor_pct=truncated_instant_vanish_max_above_floor_pct,
            max_above_floor_mirrors=truncated_instant_vanish_max_above_floor_mirrors,
        ):
            continue
        me: float | None
        try:
            me = float(mirror_eq) if mirror_eq is not None else None
        except Exception:
            me = None
        out.append(
            VanishedListing(
                fingerprint=fp,
                seller=seller,
                mirror_equiv=me,
                is_instant=instant,
            )
        )
    return out


def _canonical_price_currency(currency: Any) -> str | None:
    cur = str(currency or "").strip().lower()
    if not cur:
        return None
    if cur in {"mirror", "mirrors", "mirror of kalandra"}:
        return "mirror"
    if cur in {"divine", "divines", "div", "divine orb", "divine orbs"}:
        return "divine"
    if cur in {"exalted", "exalt", "exa", "exalted orb", "exalted orbs"}:
        return "exalted"
    return cur


def _same_listed_price(prev_signal: dict[str, Any], curr_signal: dict[str, Any]) -> bool:
    prev_amount = _as_float(prev_signal.get("priceAmount"))
    curr_amount = _as_float(curr_signal.get("priceAmount"))
    prev_currency = _canonical_price_currency(prev_signal.get("priceCurrency"))
    curr_currency = _canonical_price_currency(curr_signal.get("priceCurrency"))
    if prev_amount is None or curr_amount is None or not prev_currency or not curr_currency:
        return False
    return prev_currency == curr_currency and math.isclose(prev_amount, curr_amount, rel_tol=0.0, abs_tol=1e-9)


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


@dataclass
class _TransitionContext:
    """Inputs + accumulators shared by the per-rule steps of `evaluate_listing_transition`."""

    item_key: str
    cycle: int
    prev_signals: list[dict[str, Any]]
    curr_signals: list[dict[str, Any]]
    seller_online_probe: dict[str, bool]
    baseline_mirror: float | None
    sale_max_above_baseline_pct: float
    sale_floor_ignore_if_floor_below_mirrors: float
    sale_baseline_range_mirrors: float
    snapshot_truncated: bool
    truncation_cutoff_mirror: float | None
    truncation_safe_margin_pct: float
    truncated_instant_vanish_max_above_floor_pct: float
    truncated_instant_vanish_max_above_floor_mirrors: float
    jitter_grace: int
    online_grace: int
    prev_keys: set[tuple[str, str]] = field(default_factory=set)
    curr_keys: set[tuple[str, str]] = field(default_factory=set)
    cheapest_prev_mirror: float | None = None
    low_floor_market: bool = False
    result: InferenceCycleResult = field(default_factory=InferenceCycleResult)
    events: list[dict[str, Any]] = field(default_factory=list)

    def listing_event(
        self,
        rule: str,
        fp: str,
        seller: str,
        mirror_eq: Any,
        price_amount: Any,
        price_currency: Any,
        extra: dict[str, Any] | None = None,
        trailing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ev: dict[str, Any] = {
            "rule": rule,
            "itemKey": self.item_key,
            "fingerprint": fp,
            "seller": seller,
            "mirrorEquiv": mirror_eq,
            "priceAmount": price_amount,
            "priceCurrency": price_currency,
        }
        ev.update(extra or {})
        ev["cycle"] = self.cycle
        ev.update(trailing or {})
        return ev

    def safe_to_infer_vanish(self, mirror_eq: Any) -> bool:
        return safe_to_infer_vanish(
            mirror_eq,
            snapshot_truncated=self.snapshot_truncated,
            truncation_cutoff_mirror=self.truncation_cutoff_mirror,
            truncation_safe_margin_pct=self.truncation_safe_margin_pct,
        )

    def is_transfer(self, fp: str) -> bool:
        ps = _sellers_for_fingerprint(self.prev_signals, fp)
        cs = _sellers_for_fingerprint(self.curr_signals, fp)
        return len(ps) == 1 and len(cs) == 1 and next(iter(ps)) != next(iter(cs))

    def near_floor_for_truncated_instant_vanish(self, mirror_eq: Any) -> bool:
        return near_floor_for_truncated_instant_vanish(
            mirror_eq,
            self.cheapest_prev_mirror,
            max_above_floor_pct=self.truncated_instant_vanish_max_above_floor_pct,
            max_above_floor_mirrors=self.truncated_instant_vanish_max_above_floor_mirrors,
        )

    def unlist_guard_event(
        self, fp: str, seller: str, mirror_eq: Any, price_amount: Any, price_currency: Any
    ) -> dict[str, Any] | None:
        """Event for a removal that looks like an unlist rather than a sale, or None if sale-like."""
        if (not self.low_floor_market) and _priced_too_high_vs_baseline(
            mirror_eq,
            baseline_mirror=self.baseline_mirror,
            max_above_baseline_pct=self.sale_max_above_baseline_pct,
        ):
            return self.listing_event(
                "unlisted_above_baseline",
                fp,
                seller,
                mirror_eq,
                price_amount,
                price_currency,
                extra={
                    "baselineMirror": self.baseline_mirror,
                    "maxAboveBaselinePct": self.sale_max_above_baseline_pct,
                },
            )
        if _priced_outside_baseline_range_sub10(
            mirror_eq,
            cheapest_mirror=self.cheapest_prev_mirror,
            baseline_mirror=self.baseline_mirror,
            floor_below_mirrors=self.sale_floor_ignore_if_floor_below_mirrors,
            baseline_range_mirrors=self.sale_baseline_range_mirrors,
        ):
            return self.listing_event(
                "unlisted_above_floor_sub10",
                fp,
                seller,
                mirror_eq,
                price_amount,
                price_currency,
                extra={
                    "baselineMirror": self.baseline_mirror,
                    "floorMirror": self.cheapest_prev_mirror,
                    "floorBelowMirrors": self.sale_floor_ignore_if_floor_below_mirrors,
                    "baselineRangeMirrors": self.sale_baseline_range_mirrors,
                    "minAboveBaselineMirrors": self.sale_baseline_range_mirrors,
                    "minAboveFloorMirrors": self.sale_baseline_range_mirrors,
                },
            )
        return None


def _resolve_reappeared_pending(
    ctx: _TransitionContext,
    pend: dict[str, Any],
    *,
    fp: str,
    seller: str,
    counted_imm: bool,
    pend_grace: int,
    polls_absent: int,
    reverts_sale_rule: str,
    counter_attr: str,
) -> None:
    """Pending removal whose (fingerprint, seller) is listed again: fetch jitter or relist (rule 3)."""
    new_meta = _meta_for(ctx.curr_signals, fp, seller)
    new_price = {
        "newPriceAmount": new_meta.get("priceAmount") if new_meta else None,
        "newPriceCurrency": new_meta.get("priceCurrency") if new_meta else None,
    }
    if not counted_imm and pend_grace > 0 and polls_absent <= pend_grace:
        ctx.events.append(
            ctx.listing_event(
                "fetch_jitter_relist",
                fp,
                seller,
                pend.get("mirrorEquiv"),
                pend.get("priceAmount"),
                pend.get("priceCurrency"),
                extra={**new_price, "pollsAbsent": polls_absent, "jitterGracePolls": pend_grace},
            )
        )
        return
    ctx.result.relist_same_seller += 1
    if counted_imm:
        setattr(ctx.result, counter_attr, getattr(ctx.result, counter_attr) - 1)
    ctx.events.append(
        {
            "rule": "relist_same_seller",
            "revertsSaleRule": reverts_sale_rule,
            "itemKey": ctx.item_key,
            "fingerprint": fp,
            "seller": seller,
            "mirrorEquiv": pend.get("mirrorEquiv"),
            "priceAmount": pend.get("priceAmount"),
            "priceCurrency": pend.get("priceCurrency"),
            **new_price,
            "cycle": ctx.cycle,
        }
    )


def _resolve_pending_online(ctx: _TransitionContext, pending_online: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve pending non-instant "online" removals (rule 4b vs 3). Returns pendings still open."""
    still_pending: list[dict[str, Any]] = []
    for pend in pending_online:
        fp = str(pend.get("fingerprint") or "")
        seller = str(pend.get("seller") or "")
        removed = int(pend.get("removed_cycle") or 0)
        if not fp or not seller:
            continue
        counted_imm = bool(pend.get("countedImmediate", True))
        pend_grace = int(pend.get("jitterGracePolls") or 0)
        polls_absent = max(0, ctx.cycle - removed)
        if (fp, seller) in ctx.curr_keys:
            _resolve_reappeared_pending(
                ctx,
                pend,
                fp=fp,
                seller=seller,
                counted_imm=counted_imm,
                pend_grace=pend_grace,
                polls_absent=polls_absent,
                reverts_sale_rule="likely_non_instant_online_sale",
                counter_attr="likely_non_instant_online",
            )
            continue
        if removed >= ctx.cycle:
            still_pending.append(pend)
            continue
        if counted_imm:
            continue
        if pend_grace > 0 and polls_absent <= pend_grace:
            still_pending.append(pend)
            continue
        # Grace window elapsed with no reappearance; safe to credit the sale now.
        ctx.result.likely_non_instant_online += 1
        ctx.events.append(
            ctx.listing_event(
                "likely_non_instant_online_sale",
                fp,
                seller,
                pend.get("mirrorEquiv"),
                pend.get("priceAmount"),
                pend.get("priceCurrency"),
                trailing={"deferredCycles": polls_absent},
            )
        )
    return still_pending


def _resolve_pending_instant(ctx: _TransitionContext, pending_instant: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve older instant removals (rules 2 vs 3). Returns pendings still open."""
    still_pending: list[dict[str, Any]] = []
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
        pend_grace = int(pend.get("jitterGracePolls") or 0)
        polls_absent = max(0, ctx.cycle - removed)
        if (fp, seller) in ctx.curr_keys:
            _resolve_reappeared_pending(
                ctx,
                pend,
                fp=fp,
                seller=seller,
                counted_imm=counted_imm,
                pend_grace=pend_grace,
                polls_absent=polls_absent,
                reverts_sale_rule="likely_instant_sale",
                counter_attr="likely_instant_sale",
            )
            continue
        if removed >= ctx.cycle:
            still_pending.append(pend)
            continue
        if counted_imm:
            continue
        if pend_grace > 0 and polls_absent <= pend_grace:
            still_pending.append(pend)
            continue
        # Only resolve legacy/deferred pending-to-sale when it's safe (not a bump-out).
        if not ctx.safe_to_infer_vanish(mirror_eq):
            continue
        guard_event = ctx.unlist_guard_event(fp, seller, mirror_eq, price_amount, price_currency)
        if guard_event is not None:
            ctx.events.append(guard_event)
            continue
        ctx.result.likely_instant_sale += 1
        ctx.events.append(
            ctx.listing_event("likely_instant_sale", fp, seller, mirror_eq, price_amount, price_currency)
        )
    return still_pending


def _apply_seller_swaps(ctx: _TransitionContext) -> None:
    """Rule 1: same fingerprint moved from one sole seller to a different sole seller."""
    all_fps = set()
    for s in ctx.prev_signals:
        all_fps.add(str(s.get("fingerprint") or ""))
    for s in ctx.curr_signals:
        all_fps.add(str(s.get("fingerprint") or ""))
    all_fps.discard("")

    for fp in all_fps:
        ps = _sellers_for_fingerprint(ctx.prev_signals, fp)
        cs = _sellers_for_fingerprint(ctx.curr_signals, fp)
        if len(ps) != 1 or len(cs) != 1:
            continue
        a = next(iter(ps))
        b = next(iter(cs))
        if not (a and b and a != b):
            continue
        from_meta = _meta_for(ctx.prev_signals, fp, a) or {}
        to_meta = _meta_for(ctx.curr_signals, fp, b) or {}
        ctx.result.confirmed_transfer += 1
        ctx.events.append(
            {
                "rule": "confirmed_transfer",
                "itemKey": ctx.item_key,
                "fingerprint": fp,
                "from_seller": a,
                "to_seller": b,
                "fromMirrorEquiv": from_meta.get("mirrorEquiv"),
                "fromPriceAmount": from_meta.get("priceAmount"),
                "fromPriceCurrency": from_meta.get("priceCurrency"),
                "newMirrorEquiv": to_meta.get("mirrorEquiv"),
                "newPriceAmount": to_meta.get("priceAmount"),
                "newPriceCurrency": to_meta.get("priceCurrency"),
                "cycle": ctx.cycle,
            }
        )


def _record_non_instant_vanish(
    ctx: _TransitionContext,
    new_pending_online: list[dict[str, Any]],
    *,
    meta: dict[str, Any],
    fp: str,
    seller: str,
) -> None:
    """Rules 4 / 4b: non-instant row gone; only sale-like when the seller was online."""
    mirror_eq = meta.get("mirrorEquiv")
    price_amount = meta.get("priceAmount")
    price_currency = meta.get("priceCurrency")
    if seller in ctx.seller_online_probe:
        was_online = bool(ctx.seller_online_probe[seller])
    else:
        was_online = bool(meta.get("sellerOnline"))
    if not was_online:
        ctx.result.non_instant_removed += 1
        ctx.events.append(
            ctx.listing_event(
                "non_instant_removed_inconclusive", fp, seller, mirror_eq, price_amount, price_currency
            )
        )
        return

    guard_event = ctx.unlist_guard_event(fp, seller, mirror_eq, price_amount, price_currency)
    if guard_event is not None:
        ctx.result.non_instant_removed += 1
        ctx.events.append(guard_event)
        return

    # Defer crediting for online_grace polls: PoE's account.online flag can lag a
    # real logout, so a quick same-seller reappearance is fetch jitter, not a sale.
    defer_for_online_jitter = ctx.online_grace > 0
    if not defer_for_online_jitter:
        ctx.result.likely_non_instant_online += 1
        ctx.events.append(
            ctx.listing_event(
                "likely_non_instant_online_sale", fp, seller, mirror_eq, price_amount, price_currency
            )
        )
    else:
        ctx.events.append(
            ctx.listing_event(
                "non_instant_online_removed_pending", fp, seller, mirror_eq, price_amount, price_currency
            )
        )
    new_pending_online.append(
        {
            "fingerprint": fp,
            "seller": seller,
            "removed_cycle": ctx.cycle,
            "countedImmediate": not defer_for_online_jitter,
            "jitterGracePolls": ctx.online_grace if defer_for_online_jitter else 0,
            "mirrorEquiv": mirror_eq,
            "priceAmount": price_amount,
            "priceCurrency": price_currency,
        }
    )


def _record_instant_vanish(
    ctx: _TransitionContext,
    new_pending_instant: list[dict[str, Any]],
    *,
    meta: dict[str, Any],
    fp: str,
    seller: str,
) -> None:
    """Rules 2 / 2b: instant buyout row gone; credit now (or defer on truncated snapshots)."""
    mirror_eq = meta.get("mirrorEquiv")
    price_amount = meta.get("priceAmount")
    price_currency = meta.get("priceCurrency")
    guard_event = ctx.unlist_guard_event(fp, seller, mirror_eq, price_amount, price_currency)
    if guard_event is not None:
        ctx.events.append(guard_event)
        return

    defer_for_jitter = bool(ctx.snapshot_truncated and ctx.jitter_grace > 0)
    if not defer_for_jitter:
        ctx.result.likely_instant_sale += 1
        ctx.events.append(
            ctx.listing_event("likely_instant_sale", fp, seller, mirror_eq, price_amount, price_currency)
        )
    new_pending_instant.append(
        {
            "fingerprint": fp,
            "seller": seller,
            "removed_cycle": ctx.cycle,
            "countedImmediate": not defer_for_jitter,
            "jitterGracePolls": ctx.jitter_grace if defer_for_jitter else 0,
            "mirrorEquiv": mirror_eq,
            "priceAmount": price_amount,
            "priceCurrency": price_currency,
        }
    )
    ctx.events.append(
        ctx.listing_event(
            "instant_listing_removed_pending", fp, seller, mirror_eq, price_amount, price_currency
        )
    )


def _apply_vanishes(
    ctx: _TransitionContext,
    new_pending_instant: list[dict[str, Any]],
    new_pending_online: list[dict[str, Any]],
) -> None:
    """Vanished (fingerprint, seller) keys: new pendings + non-instant handling (rules 2, 2a, 4, 4b)."""
    vanished = ctx.prev_keys - ctx.curr_keys
    for fp, seller in vanished:
        meta = _meta_for(ctx.prev_signals, fp, seller)
        if not meta:
            continue
        instant = bool(meta.get("isInstant"))
        mirror_eq = meta.get("mirrorEquiv")

        # If we're truncated and this vanished row was near the cutoff, it may have been bumped out.
        if not ctx.safe_to_infer_vanish(mirror_eq):
            continue
        if ctx.is_transfer(fp):
            # Listing left A and appeared on B; do not treat A's disappearance as ambiguous instant removal.
            continue
        if instant and ctx.snapshot_truncated and not ctx.near_floor_for_truncated_instant_vanish(mirror_eq):
            continue

        if instant:
            _record_instant_vanish(ctx, new_pending_instant, meta=meta, fp=fp, seller=seller)
        else:
            _record_non_instant_vanish(ctx, new_pending_online, meta=meta, fp=fp, seller=seller)


def _apply_reprices(
    ctx: _TransitionContext,
    prev_pair_counts: dict[tuple[str, str], int],
    curr_pair_counts: dict[tuple[str, str], int],
) -> None:
    """Rule 5: same listing identity, listed price changed (reprice / note change)."""
    for fp, seller in ctx.prev_keys & ctx.curr_keys:
        if prev_pair_counts.get((fp, seller), 0) != 1 or curr_pair_counts.get((fp, seller), 0) != 1:
            # Multiple listings share this (fingerprint, seller) pair, so listing identity is ambiguous.
            # Skip repricing to avoid false positives from switching between distinct copies.
            continue
        pm = _meta_for(ctx.prev_signals, fp, seller)
        cm = _meta_for(ctx.curr_signals, fp, seller)
        if not pm or not cm:
            continue
        if _same_listed_price(pm, cm):
            continue
        a = _as_float(pm.get("mirrorEquiv"))
        b = _as_float(cm.get("mirrorEquiv"))
        if a is None or b is None:
            continue
        ctx.result.reprice_same_seller += 1
        ctx.events.append(
            {
                "rule": "reprice_same_seller",
                "itemKey": ctx.item_key,
                "fingerprint": fp,
                "seller": seller,
                "isInstant": bool(cm.get("isInstant")),
                "mirrorEquiv": b,
                "prevMirrorEquiv": a,
                "currMirrorEquiv": b,
                "prevPriceAmount": pm.get("priceAmount"),
                "prevPriceCurrency": pm.get("priceCurrency"),
                "currPriceAmount": cm.get("priceAmount"),
                "currPriceCurrency": cm.get("priceCurrency"),
                "cycle": ctx.cycle,
            }
        )


def _apply_count_decreases(
    ctx: _TransitionContext,
    prev_pair_counts: dict[tuple[str, str], int],
    curr_pair_counts: dict[tuple[str, str], int],
) -> None:
    """
    Rule 2c: same-seller listing-count decrease while seller still present.

    Fires when a seller's instant-buyout count for a fingerprint drops between polls but
    the seller still has at least one listing remaining (so the key never appears in
    prev_keys - curr_keys and the normal vanish path is bypassed). Typical scenario:
    seller lists N copies of the same item; one sells while the others remain.
    Not applied to truncated snapshots to avoid false positives from fetch-window bumps.
    """
    if ctx.snapshot_truncated:
        return
    for fp, seller in ctx.prev_keys & ctx.curr_keys:
        delta = prev_pair_counts.get((fp, seller), 1) - curr_pair_counts.get((fp, seller), 1)
        if delta <= 0:
            continue
        meta = _meta_for(ctx.prev_signals, fp, seller)
        if not meta or not bool(meta.get("isInstant")):
            # Non-instant multi-listing decreases are too ambiguous without an online probe.
            continue
        mirror_eq = meta.get("mirrorEquiv")
        price_amount = meta.get("priceAmount")
        price_currency = meta.get("priceCurrency")
        guard_event = ctx.unlist_guard_event(fp, seller, mirror_eq, price_amount, price_currency)
        if guard_event is not None:
            ctx.events.extend(dict(guard_event) for _ in range(delta))
            continue
        # Credit delta sales immediately without adding pending entries: the seller is still
        # listed, so (fp, seller) stays in curr_keys and a pending would be reverted as a relist.
        ctx.result.likely_instant_sale += delta
        for _ in range(delta):
            ctx.events.append(
                ctx.listing_event("likely_instant_sale", fp, seller, mirror_eq, price_amount, price_currency)
            )


def evaluate_listing_transition(
    *,
    item_key: str,
    cycle: int,
    prev_signals: list[dict[str, Any]],
    curr_signals: list[dict[str, Any]],
    pending_instant: list[dict[str, Any]],
    pending_online: list[dict[str, Any]] | None = None,
    seller_online_probe: dict[str, bool] | None = None,
    baseline_mirror: float | None = None,
    sale_max_above_baseline_pct: float = 30.0,
    sale_floor_ignore_if_floor_below_mirrors: float = 10.0,
    sale_baseline_range_mirrors: float = 1.0,
    snapshot_truncated: bool = False,
    truncation_cutoff_mirror: float | None = None,
    truncation_safe_margin_pct: float = 6.0,
    truncated_instant_vanish_max_above_floor_pct: float = 25.0,
    truncated_instant_vanish_max_above_floor_mirrors: float = 0.08,
    fetch_jitter_grace_polls: int = 2,
    non_instant_online_grace_polls: int = 1,
) -> tuple[InferenceCycleResult, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Returns (result, new_pending_instant, new_pending_online_non_instant, curr_signals_for_storage).

    Instant buyout rows that vanish (rule 2, not a transfer) credit `likely_instant_sale` on the
    same cycle. A pending entry with `countedImmediate` stays open for one more poll so a same-
    seller relist (rule 3) can decrement that credit. Legacy pendings without `countedImmediate`
    still resolve to +1 on the first later cycle where the row stays gone (old behaviour).

    Rule 2c handles the case where a seller holds multiple copies of the same item (same
    fingerprint) and sells one while keeping the others: the (fingerprint, seller) key never
    appears as vanished, but the listing count decreases. On non-truncated snapshots, a count
    drop of N instant listings credits N `likely_instant_sale` events immediately with no
    pending entry (adding a pending would cause an immediate relist-revert the next cycle
    because the seller is still present in curr_keys).

    Non-instant rows that vanish while the seller was online (rule 4b) defer for
    `non_instant_online_grace_polls` cycles before crediting `likely_non_instant_online` (the
    PoE trade `account.online` flag lags real logouts, so a same-seller reappearance within the
    grace window is treated as `fetch_jitter_relist`, not a sale + revert pair). Once the grace
    window elapses without a reappearance the sale is credited and the pending entry closes;
    a later same-seller relist is reverted by the storage layer's late-relist window
    (`StorageService.write_poll_result`), not by this engine.

    ``seller_online_probe``: optional map account name -> online bool from a live account search + fetch
    (poller). When present for a seller, overrides ``sellerOnline`` stored on the prior snapshot row.
    """
    ctx = _TransitionContext(
        item_key=item_key,
        cycle=cycle,
        prev_signals=prev_signals,
        curr_signals=curr_signals,
        seller_online_probe=seller_online_probe or {},
        baseline_mirror=baseline_mirror,
        sale_max_above_baseline_pct=sale_max_above_baseline_pct,
        sale_floor_ignore_if_floor_below_mirrors=sale_floor_ignore_if_floor_below_mirrors,
        sale_baseline_range_mirrors=sale_baseline_range_mirrors,
        snapshot_truncated=snapshot_truncated,
        truncation_cutoff_mirror=truncation_cutoff_mirror,
        truncation_safe_margin_pct=truncation_safe_margin_pct,
        truncated_instant_vanish_max_above_floor_pct=truncated_instant_vanish_max_above_floor_pct,
        truncated_instant_vanish_max_above_floor_mirrors=truncated_instant_vanish_max_above_floor_mirrors,
        jitter_grace=max(0, int(fetch_jitter_grace_polls)),
        online_grace=max(0, int(non_instant_online_grace_polls)),
    )
    ctx.prev_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in prev_signals}
    ctx.curr_keys = {(str(s["fingerprint"]), str(s["seller"])) for s in curr_signals}
    ctx.cheapest_prev_mirror = _cheapest_mirror_equiv(prev_signals)
    ctx.low_floor_market = _is_low_floor_market(
        ctx.cheapest_prev_mirror,
        floor_below_mirrors=sale_floor_ignore_if_floor_below_mirrors,
    )

    new_pending_online = _resolve_pending_online(ctx, pending_online or [])
    new_pending_instant = _resolve_pending_instant(ctx, pending_instant)
    _apply_seller_swaps(ctx)
    _apply_vanishes(ctx, new_pending_instant, new_pending_online)

    prev_pair_counts = _signal_pair_counts(prev_signals)
    curr_pair_counts = _signal_pair_counts(curr_signals)
    _apply_reprices(ctx, prev_pair_counts, curr_pair_counts)
    _apply_count_decreases(ctx, prev_pair_counts, curr_pair_counts)

    # --- Rule 6: multiple sellers listing the same roll in one ladder slice ---
    result = ctx.result
    result.multi_seller_same_fingerprint = _count_multi_seller_fingerprints(curr_signals)
    if result.multi_seller_same_fingerprint:
        ctx.events.append(
            {
                "rule": "multi_seller_same_fingerprint",
                "itemKey": item_key,
                "count": result.multi_seller_same_fingerprint,
                "cycle": cycle,
            }
        )

    # --- Rule 7: brand-new rows vs last poll ---
    result.new_listing_rows = len(ctx.curr_keys - ctx.prev_keys)
    if result.new_listing_rows and ctx.prev_keys:
        ctx.events.append(
            {
                "rule": "new_listing_rows",
                "itemKey": item_key,
                "count": result.new_listing_rows,
                "cycle": cycle,
            }
        )

    result.events = ctx.events
    return result, new_pending_instant, new_pending_online, curr_signals
