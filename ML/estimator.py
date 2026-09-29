"""
Transparent expected-return-per-day estimate for buying an item now and reselling it.

    ask            = fair value x (1 - undercut)            what we list at
    buyer flow     = shrunk sales/day x share of recent sales at >= ask
    queue          = other instant listings shown before ours, buyers take those first
    sale rate      = buyer flow / (queue + 1)
                     (optionally updated with how listings at a similar price fared; off by default)
    P(sell <= H)   = 1 - exp(-rate x H)                     exponential time-to-sale
    E[days held]   = (1 - exp(-rate x H)) / rate + lag      capped at the horizon H
    E[return]      = P x (ask x (1 - fee) / entry - 1) + (1 - P) x unsold return
    return / day   = E[return] / E[days held]

Sales split into two channels by listing currency (see ML/features.py), and each plan only
counts its own channel's buyers. The plan with the best return/day wins:
- "undercut": list in divines just under the divine-channel fair value (above). The queue is the
  other divine listings at or below our ask plus the whole-mirror listings worth <= our ask, which
  the trade site shows first.
- "mirror": list at exactly k whole mirrors (k above the entry price). Buyer flow comes only from
  past whole-mirror sales at >= k mirrors; the queue is every whole-mirror listing at <= k (buyers
  pick among equal whole-mirror listings at random, so equal ones count as ahead on average).
  Candidates are the next whole mirror above the entry, plus the k's this item actually sold at
  up to 1.5x its value.

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
    # An unsold copy is worth less at the horizon: on the Sep 2026 production DB, marking every copy at
    # min(later floor, entry) after 60 days lost ~10% on average (median 0, steady across weeks). Without
    # this, a long-shot listing (e.g. far above value in whole mirrors) looked like a free lottery ticket.
    unsold_markdown_pct_per_30d: float = 5.0
    # Each cheaper-or-equal instant listing is sold before ours.
    use_queue: bool = True
    # Update the sale rate with sold/unsold listing episodes priced near our ask. Off by default: on the
    # Sep 2026 production DB it pushed the predicted sell chance to ~45% where ~77% actually sold
    # (it measures how fast an *average* listing at that price sells, while ours is the cheapest).
    # The same counts are still passed to the learned model as features.
    use_listing_evidence: bool = False
    evidence_band: tuple[float, float] = (0.9, 1.25)  # "similar price" relative to our ask
    evidence_prior_listing_days: float = 60.0  # pseudo listing-days behind the flow-based rate
    consider_mirror_plan: bool = True  # also evaluate listing at exactly k mirrors (see module docstring)
    # For items worth under 1 mirror, most recorded 1-mirror "sales" are listings withdrawn, not bought:
    # on the Sep 2026 production DB the 1-mirror plan predicted a 10% sell chance where 1.6% sold
    # (3 of 192 item-weeks). Those sales count at this weight. Items worth more were calibrated.
    premium_mirror_sale_weight: float = 0.15
    # Anchor the ask on what is listed now: never list behind a cheaper competing copy. The divine ask
    # is at most just under the cheapest competing listing; a whole-mirror ask at most the cheapest
    # competing whole-mirror price (ties allowed: buyers pick among equal ones at random). Past sales
    # still cap the ask (fair value) and set how fast buyers come. Prices drop in this market, and a
    # copy behind cheaper ones mostly waits for them.
    front_of_queue: bool = True
    # With front_of_queue: join a whole-mirror price only when at most this many other copies are
    # listed at it. Buyers pick among equal listings at random, but per the market owner a crowd at the
    # same price (e.g. 6+ Death Rush at 2 mirrors) mostly doesn't sell. The backtest preferred allowing
    # any tie (+0.32%/day vs +0.21%, 75% vs 82% sold); this follows the owner's read of the market.
    max_equal_whole_mirror_listings: int = 2


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
    plan: str = "undercut"  # "undercut" (priced in divines) or "mirror" (exactly ask_whole_mirrors mirrors)
    ask_whole_mirrors: int = 0

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
        unsold_markdown_pct_per_30d=defaults.unsold_markdown_pct_per_30d,
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
    n_channel: int,
    ask_whole_mirrors: int = 0,
) -> Estimate:
    fee = max(0.0, params.fee_pct) / 100.0
    horizon = max(1.0, params.horizon_days)
    rate = max(1e-6, rate)
    p_sell = 1.0 - math.exp(-rate * horizon)
    expected_days = min(p_sell / rate + max(0.0, params.listing_lag_days), horizon + params.listing_lag_days)

    return_if_sold = ask * (1.0 - fee) / snap.entry_price - 1.0
    # Unsold at the horizon: still holding a copy worth roughly the cheaper of entry/fair, marked down.
    markdown = min(0.9, max(0.0, params.unsold_markdown_pct_per_30d) / 100.0 * horizon / 30.0)
    return_if_unsold = min(snap.entry_price, snap.fair_value) * (1.0 - markdown) * (1.0 - fee) / snap.entry_price - 1.0
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
        confidence=confidence_tier(n_channel),
        sales_at_or_above_ask_90d=n_at_ask,
        queue_ahead=queue,
        similar_listing_days_90d=exposure,
        similar_listings_sold_90d=sold,
        plan=plan,
        ask_whole_mirrors=ask_whole_mirrors,
    )


FRONT_STEP = 0.002  # how far under the cheapest competing listing a divine ask goes (~1 div at 500 div)


def _competing_divine(snap: Snapshot) -> list[float]:
    """Divine-channel listings other than the one we buy."""
    ladder = list(snap.divine_ladder)
    return ladder[1:] if snap.entry_whole_mirrors == 0 and ladder else ladder


def _competing_whole(snap: Snapshot) -> list[tuple[int, int]]:
    """Whole-mirror listings ((k, count), ...) other than the one we buy."""
    out = []
    for k, c in snap.mirror_listings:
        if k == snap.entry_whole_mirrors:
            c -= 1
        if c > 0:
            out.append((k, c))
    return out


def _undercut_plan(snap: Snapshot, params: EstimatorParams) -> Estimate:
    ask = snap.fair_value * (1.0 - max(0.0, params.undercut_pct) / 100.0)
    if params.front_of_queue:
        cheapest = min(_competing_divine(snap) + [float(k) for k, _ in _competing_whole(snap)], default=None)
        if cheapest is not None:
            ask = min(ask, cheapest * (1.0 - FRONT_STEP))
    n90 = snap.sales_90d
    n_at_ask = sum(1 for p in snap.recent_sale_prices if p >= ask - 1e-9)

    b = max(0.0, params.prior_exposure_days)
    all_sales_rate = (n90 + snap.market_sale_rate_per_day * b) / (SALE_WINDOW_DAYS + b)
    share_at_ask = (n_at_ask + 0.5) / (n90 + 1.0)
    rate = max(1e-6, all_sales_rate * share_at_ask)

    queue = 0
    if params.use_queue:
        # Divine listings at or below our ask, and whole-mirror listings worth <= our ask (the site
        # shows those first), sell before ours. The listing we buy is not in the queue.
        queue = sum(1 for p in snap.divine_ladder if p <= ask + 1e-9)
        queue += sum(c for k, c in snap.mirror_listings if k <= ask + 1e-9)
        if snap.entry_whole_mirrors == 0 and snap.divine_ladder and snap.divine_ladder[0] <= ask + 1e-9:
            queue -= 1
        elif snap.entry_whole_mirrors and snap.entry_whole_mirrors <= ask + 1e-9:
            queue -= 1
        queue = max(0, queue)
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

    return _finish(
        snap, params, ask=ask, rate=rate, plan="undercut", n_at_ask=n_at_ask, queue=queue, exposure=exposure, sold=sold,
        n_channel=n90,
    )


# Whole-mirror asks above this multiple of the item's value are not considered (other than the next
# whole mirror). On the Sep 2026 production DB, "sales" far above value (e.g. 100 mirrors for a
# 20-mirror item) were mostly withdrawn fantasy listings: those picks never sold, while round-ups
# (20 -> 21, 10 -> 13, 2 -> 3, 0.43 -> 1) did.
MIRROR_PLAN_MAX_MARKUP = 1.5


def mirror_candidates(snap: Snapshot) -> list[int]:
    """Whole-mirror asks worth trying: the next whole mirror above the entry price, plus past
    whole-mirror sale amounts above the entry and within MIRROR_PLAN_MAX_MARKUP of value."""
    cap = MIRROR_PLAN_MAX_MARKUP * max(snap.entry_price, snap.fair_value)
    ks = {k for k in snap.mirror_sale_amounts if snap.entry_price + 1e-9 < k <= cap + 1e-9}
    ks.add(math.floor(snap.entry_price + 1e-9) + 1)
    return sorted(ks)


def _mirror_plan(snap: Snapshot, params: EstimatorParams, k: int) -> Estimate:
    amounts = snap.mirror_sale_amounts
    n = len(amounts)
    n_at_k = sum(1 for a in amounts if a >= k)
    # Prior: the pooled whole-mirror sale rate across items, far below the divine-channel rate.
    b = max(0.0, params.prior_exposure_days)
    all_rate = (n + snap.market_mirror_rate_per_day * b) / (SALE_WINDOW_DAYS + b)
    rate = all_rate * (n_at_k + 0.5) / (n + 1.0)
    if snap.entry_price < 1.0:
        rate *= max(0.0, params.premium_mirror_sale_weight)
    queue = 0
    if params.use_queue:
        # Cheaper whole-mirror listings sell first; buyers pick among equal ones at random.
        queue = sum(c for kk, c in snap.mirror_listings if kk <= k)
        if snap.entry_whole_mirrors and snap.entry_whole_mirrors <= k:
            queue -= 1  # the listing we buy
        queue = max(0, queue)
        rate = rate / (queue + 1)
    return _finish(
        snap, params, ask=float(k), rate=rate, plan="mirror", n_at_ask=n_at_k, queue=queue, exposure=0.0, sold=0,
        n_channel=n_at_k, ask_whole_mirrors=k,
    )


def estimate(snap: Snapshot, params: EstimatorParams = EstimatorParams()) -> Estimate:
    best = _undercut_plan(snap, params)
    # No whole-mirror sales anywhere means no evidence that mirror buyers exist.
    if params.consider_mirror_plan and (snap.mirror_sale_amounts or snap.market_mirror_rate_per_day > 0):
        competing = dict(_competing_whole(snap))
        max_k = min(competing, default=None) if params.front_of_queue else None
        for k in mirror_candidates(snap):
            if max_k is not None and (k > max_k or competing.get(k, 0) > params.max_equal_whole_mirror_listings):
                continue
            alt = _mirror_plan(snap, params, k)
            if alt.return_per_day > best.return_per_day:
                best = alt
    return best
