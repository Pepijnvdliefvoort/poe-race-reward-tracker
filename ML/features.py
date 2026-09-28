from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Any

from ML.market import Market, VariantHistory

DAY = 86400.0

# Point-in-time window lengths (days).
SALE_WINDOW_DAYS = 90
RECENT_SALE_DAYS = 30
LISTING_ANCHOR_DAYS = 30
MAX_FLOOR_AGE_DAYS = 7  # older floors are not a buyable price any more
SALE_HALF_LIFE_DAYS = 30.0
FAIR_VALUE_SHRINK_K = 3.0  # sales needed before the sale anchor outweighs listing floors
# Below this normal price level, a listing at exactly 1 mirror is a premium sold to buyers who don't
# compare prices (slow, separate channel). Above it, a 1-mirror sale is just an ordinary sale.
ONE_MIRROR_PREMIUM_BELOW = 0.9


@dataclass(frozen=True)
class Snapshot:
    """Everything known about one variant at time `ts`, using data <= ts only."""

    variant_id: int
    ts: float
    entry_price: float  # cheapest listing at ts (what you'd pay now)
    entry_age_days: float
    listing_anchor: float | None  # median floor over the last 30 days
    sale_anchor: float | None  # recency-weighted median sale price over the last 90 days
    fair_value: float  # sale anchor shrunk toward the listing anchor when sales are sparse
    sales_30d: int
    sales_90d: int
    recent_sale_prices: tuple[float, ...]  # last 90 days, for "sales at >= my ask" counts
    days_since_last_sale: float | None
    total_listings: int
    listings_change_30d: float | None  # listings now / listings ~30 days ago - 1
    new_listings_per_day_30d: float
    sale_momentum: float | None  # sale anchor (last 30d) / sale median (30-90d ago) - 1
    floor_momentum: float | None  # entry price / median floor 30-60 days ago - 1
    market_sale_rate_per_day: float  # pooled prior across all variants at ts
    # Cheapest instant listings at the entry poll, ascending (index 0 is the one we would buy).
    instant_ladder: tuple[float, ...] = ()
    # Instant listing episodes overlapping the last 90 days: (price, days listed in window, sold by ts).
    listing_evidence: tuple[tuple[float, float, bool], ...] = ()
    # 1-mirror channel (only when 1 mirror is a premium for this item; its sales are then excluded
    # from the normal-channel fields above).
    one_mirror_premium: bool = False
    one_mirror_sales_90d: int = 0
    one_mirror_listings: int = 0
    market_one_mirror_rate_per_day: float = 0.0  # pooled 1-mirror sale rate per premium variant (prior)

    def as_feature_row(self) -> dict[str, float]:
        """Numeric model inputs (NaN = unknown; the model handles missing values)."""

        def f(x: Any) -> float:
            return float(x) if x is not None and math.isfinite(float(x)) else float("nan")

        return {
            "log_entry_price": math.log(self.entry_price),
            "entry_to_fair": self.entry_price / self.fair_value - 1.0,
            "entry_to_sale_anchor": f(self.entry_price / self.sale_anchor - 1.0) if self.sale_anchor else float("nan"),
            "sales_30d": float(self.sales_30d),
            "sales_90d": float(self.sales_90d),
            "days_since_last_sale": f(min(self.days_since_last_sale, 365.0)) if self.days_since_last_sale is not None else float("nan"),
            "log1p_listings": math.log1p(self.total_listings),
            "listings_change_30d": f(self.listings_change_30d),
            "new_listings_per_day_30d": self.new_listings_per_day_30d,
            "sale_momentum": f(self.sale_momentum),
            "floor_momentum": f(self.floor_momentum),
            "entry_age_days": self.entry_age_days,
            "market_sale_rate_per_day": self.market_sale_rate_per_day,
            "one_mirror_premium": float(self.one_mirror_premium),
            "one_mirror_sales_90d": float(self.one_mirror_sales_90d),
            "one_mirror_listings": float(self.one_mirror_listings),
        }


FEATURE_NAMES: list[str] = [
    "log_entry_price",
    "entry_to_fair",
    "entry_to_sale_anchor",
    "sales_30d",
    "sales_90d",
    "days_since_last_sale",
    "log1p_listings",
    "listings_change_30d",
    "new_listings_per_day_30d",
    "sale_momentum",
    "floor_momentum",
    "entry_age_days",
    "market_sale_rate_per_day",
    "one_mirror_premium",
    "one_mirror_sales_90d",
    "one_mirror_listings",
]


def _weighted_median(values: list[tuple[float, float]]) -> float | None:
    clean = sorted((v, w) for v, w in values if w > 0)
    if not clean:
        return None
    half = sum(w for _, w in clean) / 2.0
    acc = 0.0
    for v, w in clean:
        acc += w
        if acc >= half:
            return v
    return clean[-1][0]


def _median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def market_sale_rate(market: Market, ts: float) -> float:
    """Pooled average sales/day per variant over the last 90 days (prior for sparse items)."""
    rates: list[float] = []
    lo = ts - SALE_WINDOW_DAYS * DAY
    for hist in market.variants.values():
        if hist.polls_upto(ts) == 0:
            continue
        n = hist.sales_upto(ts) - hist.sales_upto(lo)
        rates.append(n / SALE_WINDOW_DAYS)
    if not rates:
        return 1.0 / 365.0
    return max(1.0 / 365.0, sum(rates) / len(rates))


def market_one_mirror_rate(market: Market, ts: float) -> float:
    """Pooled 1-mirror sales/day per variant over the last 90 days (prior for the 1-mirror plan)."""
    lo = ts - SALE_WINDOW_DAYS * DAY
    n_variants = 0
    n_sales = 0
    for hist in market.variants.values():
        if hist.polls_upto(ts) == 0:
            continue
        n_variants += 1
        n_sales += sum(1 for s in hist.sales[hist.sales_upto(lo):hist.sales_upto(ts)] if s.one_mirror)
    return n_sales / (n_variants * SALE_WINDOW_DAYS) if n_variants else 0.0


def snapshot(hist: VariantHistory, ts: float, *, market_rate: float, market_one_mirror: float = 0.0) -> Snapshot | None:
    """Point-in-time view of `hist` at `ts`, or None if there is no recent buyable price."""
    n_polls = hist.polls_upto(ts)
    if n_polls == 0:
        return None
    polls = hist.polls[:n_polls]

    # Buy price = cheapest instant-buyout listing: non-instant floors are often unresponsive sellers.
    latest_buyable = next((p for p in reversed(polls) if p.instant_floor is not None), None)
    if latest_buyable is None:
        return None
    entry_age = (ts - latest_buyable.ts) / DAY
    if entry_age > MAX_FLOOR_AGE_DAYS:
        return None
    entry = float(latest_buyable.instant_floor)
    ladder = tuple(float(p) for p in latest_buyable.instant_ladder)

    def floors_between(lo_days: float, hi_days: float) -> list[float]:
        lo, hi = ts - lo_days * DAY, ts - hi_days * DAY
        return [p.floor_mirror for p in polls if lo < p.ts <= hi and p.floor_mirror is not None]

    listing_anchor = _median_or_none(floors_between(LISTING_ANCHOR_DAYS, 0))
    older_floor = _median_or_none(floors_between(60, 30))

    premium = (listing_anchor if listing_anchor is not None else entry) < ONE_MIRROR_PREMIUM_BELOW

    def normal_channel(sale) -> bool:
        return not (premium and sale.one_mirror)

    n_sales = hist.sales_upto(ts)
    lo90 = ts - SALE_WINDOW_DAYS * DAY
    window = hist.sales[hist.sales_upto(lo90):n_sales]
    recent = [s for s in window if normal_channel(s)]
    one_mirror_sales = len(window) - len(recent)
    recent_30 = [s for s in recent if s.ts > ts - RECENT_SALE_DAYS * DAY]
    older_sales = [s.price_mirror for s in recent if s.ts <= ts - RECENT_SALE_DAYS * DAY]

    sale_anchor = _weighted_median(
        [(s.price_mirror, 0.5 ** (((ts - s.ts) / DAY) / SALE_HALF_LIFE_DAYS)) for s in recent]
    )
    recent_anchor = _median_or_none([s.price_mirror for s in recent_30])
    older_anchor = _median_or_none(older_sales)
    sale_momentum = (recent_anchor / older_anchor - 1.0) if recent_anchor and older_anchor else None

    listing_ref = listing_anchor if listing_anchor is not None else entry
    if sale_anchor is not None:
        w = len(recent) / (len(recent) + FAIR_VALUE_SHRINK_K)
        fair = w * sale_anchor + (1.0 - w) * listing_ref
    else:
        fair = listing_ref

    last_normal = next((s for s in reversed(hist.sales[:n_sales]) if normal_channel(s)), None)
    days_since_last_sale = (ts - last_normal.ts) / DAY if last_normal else None

    listings_now = polls[-1].total_results
    past_listings = [p.total_results for p in polls if ts - 37 * DAY < p.ts <= ts - 23 * DAY]
    past_median = _median_or_none([float(x) for x in past_listings])
    listings_change = (listings_now / past_median - 1.0) if past_median else None

    new_rows_30 = sum(p.new_listing_rows for p in polls if p.ts > ts - 30 * DAY)
    first_ts = polls[0].ts
    observed_days = max(1.0, min(30.0, (ts - first_ts) / DAY))

    evidence = []
    for ep in hist.episodes:
        if ep.start_ts > ts:
            break  # episodes are sorted by start
        if not ep.instant:
            continue
        lo, hi = max(ep.start_ts, lo90), min(ep.end_ts, ts)
        if hi <= lo:
            continue
        evidence.append((ep.price_mirror, (hi - lo) / DAY, ep.sold and ep.end_ts <= ts))

    return Snapshot(
        variant_id=hist.variant_id,
        ts=ts,
        entry_price=entry,
        entry_age_days=round(entry_age, 4),
        listing_anchor=listing_anchor,
        sale_anchor=sale_anchor,
        fair_value=fair,
        sales_30d=len(recent_30),
        sales_90d=len(recent),
        recent_sale_prices=tuple(s.price_mirror for s in recent),
        days_since_last_sale=days_since_last_sale,
        total_listings=listings_now,
        listings_change_30d=listings_change,
        new_listings_per_day_30d=new_rows_30 / observed_days,
        sale_momentum=sale_momentum,
        floor_momentum=(entry / older_floor - 1.0) if older_floor else None,
        market_sale_rate_per_day=market_rate,
        instant_ladder=ladder,
        listing_evidence=tuple(evidence),
        one_mirror_premium=premium,
        one_mirror_sales_90d=one_mirror_sales,
        one_mirror_listings=latest_buyable.one_mirror_listings,
        market_one_mirror_rate_per_day=market_one_mirror,
    )


def snapshots_at(market: Market, ts: float) -> list[Snapshot]:
    rate = market_sale_rate(market, ts)
    one_mirror_rate = market_one_mirror_rate(market, ts)
    out = []
    for hist in market.variants.values():
        snap = snapshot(hist, ts, market_rate=rate, market_one_mirror=one_mirror_rate)
        if snap is not None:
            out.append(snap)
    return out
