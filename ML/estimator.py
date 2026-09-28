"""
Transparent expected-return-per-day estimate for buying an item now and reselling it.

    ask            = fair value x (1 - undercut)            what we list at
    buyer flow     = shrunk sales/day x share of recent sales at >= ask
    queue          = other instant listings priced <= ask   buyers take those first
    sale rate      = buyer flow / (queue + 1)
                     (optionally updated with how listings at a similar price fared; off by default)
    P(sell <= H)   = 1 - exp(-rate x H)                     exponential time-to-sale
    E[days held]   = (1 - exp(-rate x H)) / rate + lag      capped at the horizon H
    E[return]      = P x (ask x (1 - fee) / entry - 1) + (1 - P) x unsold return
    return / day   = E[return] / E[days held]

Two listing plans are evaluated and the better return/day wins:
- "undercut": list just under the normal-channel fair value (above).
- "one_mirror": for items normally worth under ~0.9 mirror, list at exactly 1 mirror. Some buyers
  take 1-mirror listings without comparing prices, but slowly: the rate comes only from past
  1-mirror sales, and every existing 1-mirror listing is assumed to sell before ours.

Sparse items borrow strength from the market-wide sale rate, so one lucky sale does not make a
rare item look liquid. Everything is in % of the entry price, so cheap and expensive items rank
on the same scale.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from ML.features import SALE_WINDOW_DAYS, Snapshot


@dataclass(frozen=True)
class EstimatorParams:
    horizon_days: float = 60.0
    fee_pct: float = 0.0  # trading cost as % of the sale price
    # List this far below fair value. On real data (Sep 2026) 5% beat 2% at both 30d and 60d horizons:
    # in a thin market, a slightly lower ask sells noticeably faster.
    undercut_pct: float = 5.0
    # Weight (in days) of the market-wide sale rate for sparse items. 30 had the best sell-chance
    # calibration on the cleaned production DB (predicted ~54% vs ~56% actual); 60 pulled busy items
    # down too hard, 15 ranked slightly better but was less calibrated (differences within noise).
    prior_exposure_days: float = 30.0
    listing_lag_days: float = 0.5  # time to list + first buyer to notice
    # Each cheaper-or-equal instant listing is sold before ours.
    use_queue: bool = True
    # Update the sale rate with sold/unsold listing episodes priced near our ask. Off by default: on the
    # Sep 2026 production DB it pushed the predicted sell chance to ~45% where ~77% actually sold
    # (it measures how fast an *average* listing at that price sells, while ours is the cheapest).
    # The same counts are still passed to the learned model as features.
    use_listing_evidence: bool = False
    evidence_band: tuple[float, float] = (0.9, 1.25)  # "similar price" relative to our ask
    evidence_prior_listing_days: float = 60.0  # pseudo listing-days behind the flow-based rate
    consider_one_mirror: bool = True  # also evaluate listing at exactly 1 mirror (see module docstring)


@dataclass(frozen=True)
class Estimate:
    ask_price: float
    sale_rate_per_day: float
    sell_probability: float
    expected_days: float
    return_if_sold: float
    return_if_unsold: float
    expected_return: float
    return_per_day: float
    confidence: str
    sales_at_or_above_ask_90d: int
    queue_ahead: int = 0
    similar_listing_days_90d: float = 0.0
    similar_listings_sold_90d: int = 0
    plan: str = "undercut"  # "undercut" or "one_mirror"

    def as_dict(self) -> dict:
        return asdict(self)


def params_from_config(cfg: dict | None) -> EstimatorParams:
    """Read `invest_*` keys from the market app config (shared by the server and the retrain)."""
    cfg = cfg or {}
    defaults = EstimatorParams()

    def num(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return default if not math.isfinite(v) else max(lo, min(hi, v))

    return EstimatorParams(
        horizon_days=num("invest_horizon_days", defaults.horizon_days, 7.0, 180.0),
        fee_pct=num("invest_fee_pct", defaults.fee_pct, 0.0, 50.0),
        undercut_pct=num("invest_undercut_pct", defaults.undercut_pct, 0.0, 50.0),
        prior_exposure_days=defaults.prior_exposure_days,
        listing_lag_days=defaults.listing_lag_days,
        use_queue=defaults.use_queue,
        use_listing_evidence=defaults.use_listing_evidence,
        evidence_band=defaults.evidence_band,
        evidence_prior_listing_days=defaults.evidence_prior_listing_days,
    )


def confidence_tier(sales_90d: int) -> str:
    if sales_90d >= 5:
        return "strong"
    if sales_90d >= 2:
        return "medium"
    return "sparse"


def _finish(
    snap: Snapshot,
    params: EstimatorParams,
    *,
    ask: float,
    rate: float,
    plan: str,
    n_at_ask: int,
    queue: int,
    exposure: float,
    sold: int,
) -> Estimate:
    fee = max(0.0, params.fee_pct) / 100.0
    horizon = max(1.0, params.horizon_days)
    rate = max(1e-6, rate)
    p_sell = 1.0 - math.exp(-rate * horizon)
    expected_days = min(p_sell / rate + max(0.0, params.listing_lag_days), horizon + params.listing_lag_days)

    return_if_sold = ask * (1.0 - fee) / snap.entry_price - 1.0
    # Unsold at the horizon: still holding a copy worth roughly the cheaper of entry/fair.
    return_if_unsold = min(snap.entry_price, snap.fair_value) * (1.0 - fee) / snap.entry_price - 1.0
    expected_return = p_sell * return_if_sold + (1.0 - p_sell) * return_if_unsold
    return Estimate(
        ask_price=ask,
        sale_rate_per_day=rate,
        sell_probability=p_sell,
        expected_days=expected_days,
        return_if_sold=return_if_sold,
        return_if_unsold=return_if_unsold,
        expected_return=expected_return,
        return_per_day=expected_return / expected_days,
        confidence=confidence_tier(snap.one_mirror_sales_90d if plan == "one_mirror" else snap.sales_90d),
        sales_at_or_above_ask_90d=n_at_ask,
        queue_ahead=queue,
        similar_listing_days_90d=exposure,
        similar_listings_sold_90d=sold,
        plan=plan,
    )


def _undercut_plan(snap: Snapshot, params: EstimatorParams) -> Estimate:
    ask = snap.fair_value * (1.0 - max(0.0, params.undercut_pct) / 100.0)
    n90 = snap.sales_90d
    n_at_ask = sum(1 for p in snap.recent_sale_prices if p >= ask - 1e-9)

    b = max(0.0, params.prior_exposure_days)
    all_sales_rate = (n90 + snap.market_sale_rate_per_day * b) / (SALE_WINDOW_DAYS + b)
    share_at_ask = (n_at_ask + 0.5) / (n90 + 1.0)
    rate = max(1e-6, all_sales_rate * share_at_ask)

    queue = 0
    if params.use_queue and snap.instant_ladder:
        # Index 0 is the listing we buy; everything else at or below our ask sells first.
        queue = sum(1 for p in snap.instant_ladder[1:] if p <= ask + 1e-9)
        rate = rate / (queue + 1)

    exposure = 0.0
    sold = 0
    if snap.listing_evidence:
        lo, hi = ask * params.evidence_band[0], ask * params.evidence_band[1]
        for price, days, was_sold in snap.listing_evidence:
            if lo <= price <= hi:
                exposure += days
                sold += int(was_sold)
    if params.use_listing_evidence and exposure > 0:
        # Gamma-Poisson update: the flow/queue rate is the prior, listing-days are the evidence.
        b_ev = max(1e-6, params.evidence_prior_listing_days)
        rate = max(1e-6, (sold + rate * b_ev) / (exposure + b_ev))

    return _finish(snap, params, ask=ask, rate=rate, plan="undercut", n_at_ask=n_at_ask, queue=queue, exposure=exposure, sold=sold)


def _one_mirror_plan(snap: Snapshot, params: EstimatorParams) -> Estimate | None:
    if not (params.consider_one_mirror and snap.one_mirror_premium) or snap.entry_price >= 1.0:
        return None
    # Prior: the pooled 1-mirror sale rate across items, which is far below the normal sale rate.
    b = max(0.0, params.prior_exposure_days)
    rate = (snap.one_mirror_sales_90d + snap.market_one_mirror_rate_per_day * b) / (SALE_WINDOW_DAYS + b)
    # Every existing 1-mirror listing is assumed to be shown and sold before ours.
    queue = snap.one_mirror_listings if params.use_queue else 0
    rate = rate / (queue + 1)
    return _finish(
        snap, params, ask=1.0, rate=rate, plan="one_mirror", n_at_ask=snap.one_mirror_sales_90d, queue=queue, exposure=0.0, sold=0
    )


def estimate(snap: Snapshot, params: EstimatorParams = EstimatorParams()) -> Estimate:
    best = _undercut_plan(snap, params)
    alt = _one_mirror_plan(snap, params)
    if alt is not None and alt.return_per_day > best.return_per_day:
        best = alt
    return best
