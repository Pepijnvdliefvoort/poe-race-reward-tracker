"""
Transparent expected-return-per-day estimate for buying an item now and reselling it.

    ask            = fair value x (1 - undercut)            what we list at
    sale rate      = shrunk sales/day x share of recent sales at >= ask
    P(sell <= H)   = 1 - exp(-rate x H)                     exponential time-to-sale
    E[days held]   = (1 - exp(-rate x H)) / rate + lag      capped at the horizon H
    E[return]      = P x (ask x (1 - fee) / entry - 1) + (1 - P) x unsold return
    return / day   = E[return] / E[days held]

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
    undercut_pct: float = 2.0  # list this far below fair value to be the cheapest seller
    prior_exposure_days: float = 60.0  # weight of the market-wide sale rate for sparse items
    listing_lag_days: float = 0.5  # time to list + first buyer to notice


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
    )


def confidence_tier(sales_90d: int) -> str:
    if sales_90d >= 5:
        return "strong"
    if sales_90d >= 2:
        return "medium"
    return "sparse"


def estimate(snap: Snapshot, params: EstimatorParams = EstimatorParams()) -> Estimate:
    fee = max(0.0, params.fee_pct) / 100.0
    horizon = max(1.0, params.horizon_days)

    ask = snap.fair_value * (1.0 - max(0.0, params.undercut_pct) / 100.0)
    n90 = snap.sales_90d
    n_at_ask = sum(1 for p in snap.recent_sale_prices if p >= ask - 1e-9)

    b = max(0.0, params.prior_exposure_days)
    all_sales_rate = (n90 + snap.market_sale_rate_per_day * b) / (SALE_WINDOW_DAYS + b)
    share_at_ask = (n_at_ask + 0.5) / (n90 + 1.0)
    rate = max(1e-6, all_sales_rate * share_at_ask)

    p_sell = 1.0 - math.exp(-rate * horizon)
    expected_days = p_sell / rate + max(0.0, params.listing_lag_days)
    expected_days = min(expected_days, horizon + params.listing_lag_days)

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
        confidence=confidence_tier(n90),
        sales_at_or_above_ask_90d=n_at_ask,
    )
