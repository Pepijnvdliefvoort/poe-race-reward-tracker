"""
Trading simulation on recorded history.

At each weekly decision time a strategy ranks the variants; we "buy" the top picks at their floor
price and list them at the estimator's ask price. A position sells at the first later recorded
sale at or above our ask (a buyer who paid that much would have taken our cheaper copy). If no
such sale happens within the horizon, the position is marked to market at the floor price then.

Only decision times whose full horizon lies inside the recorded data are used, so every outcome
is fully observed (no right-censoring).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from ML.estimator import EstimatorParams, estimate
from ML.features import DAY, MAX_FLOOR_AGE_DAYS, Snapshot, snapshots_at
from ML.market import Market, VariantHistory

MIN_HOLD_DAYS = 0.125  # one poll interval: a sale can't be observed sooner than the next poll

# A scorer maps the snapshots at one decision time to a score per variant (higher = better).
# Variants with a score <= 0 (or missing) are never bought.
Scorer = Callable[[list[Snapshot]], dict[int, float]]


@dataclass(frozen=True)
class TradeOutcome:
    variant_id: int
    entry_ts: float
    entry_price: float
    ask_price: float
    sold: bool
    days: float
    ret: float

    @property
    def return_per_day(self) -> float:
        return self.ret / self.days


def decision_times(market: Market, *, horizon_days: float, warmup_days: float = 30.0, step_days: float = 7.0) -> list[float]:
    if market.start_ts is None or market.end_ts is None:
        return []
    out = []
    t = market.start_ts + warmup_days * DAY
    last = market.end_ts - horizon_days * DAY
    while t <= last:
        out.append(t)
        t += step_days * DAY
    return out


def realize_trade(hist: VariantHistory, snap: Snapshot, *, ask: float, horizon_days: float, fee_pct: float) -> TradeOutcome:
    fee = max(0.0, fee_pct) / 100.0
    end = snap.ts + horizon_days * DAY
    first = hist.sales_upto(snap.ts)
    last = hist.sales_upto(end)
    for sale in hist.sales[first:last]:
        if sale.price_mirror >= ask - 1e-9:
            days = max(MIN_HOLD_DAYS, (sale.ts - snap.ts) / DAY)
            return TradeOutcome(snap.variant_id, snap.ts, snap.entry_price, ask, True, days, ask * (1 - fee) / snap.entry_price - 1)

    # Unsold: value at the later floor, but never above what we paid. A risen floor is not a
    # realized gain (often just the cheap listing we bought disappearing), a fallen one is a loss.
    n_polls = hist.polls_upto(end)
    exit_value = snap.entry_price  # no usable later floor: assume flat
    for p in reversed(hist.polls[:n_polls]):
        if p.ts <= snap.ts:
            break
        later_floor = p.instant_floor if p.instant_floor is not None else p.floor_mirror
        if later_floor is not None and (end - p.ts) / DAY <= MAX_FLOOR_AGE_DAYS:
            exit_value = min(later_floor, snap.entry_price)
            break
    return TradeOutcome(
        snap.variant_id, snap.ts, snap.entry_price, ask, False, horizon_days, exit_value * (1 - fee) / snap.entry_price - 1
    )


@dataclass
class StrategyResult:
    name: str
    trades: list[TradeOutcome] = field(default_factory=list)
    per_decision: dict[float, float] = field(default_factory=dict)  # decision ts -> return/day

    def summary(self) -> dict:
        n = len(self.trades)
        if not n:
            return {"name": self.name, "trades": 0}
        total_days = sum(t.days for t in self.trades)
        total_ret = sum(t.ret for t in self.trades)
        sold = [t for t in self.trades if t.sold]
        return {
            "name": self.name,
            "trades": n,
            "decisions": len(self.per_decision),
            # Equal capital per trade: total % gained / total days capital was tied up.
            "returnPerDay": total_ret / total_days,
            "meanReturnPerTrade": total_ret / n,
            "soldRate": len(sold) / n,
            "meanDaysHeld": total_days / n,
            "meanDaysToSellWhenSold": (sum(t.days for t in sold) / len(sold)) if sold else None,
        }


def estimator_scorer(params: EstimatorParams) -> Scorer:
    return lambda snaps: {s.variant_id: estimate(s, params).return_per_day for s in snaps}


def random_scorer(seed: int = 1) -> Scorer:
    rng = random.Random(seed)
    return lambda snaps: {s.variant_id: rng.random() for s in snaps}


def run_backtest(
    market: Market,
    strategies: dict[str, Scorer],
    *,
    params: EstimatorParams = EstimatorParams(),
    times: list[float] | None = None,
    top_k: int = 5,
    snapshots_by_time: dict[float, list[Snapshot]] | None = None,
) -> dict[str, StrategyResult]:
    """
    Evaluate strategies on the same decision times. Every strategy exits with the estimator's
    ask price, so the comparison isolates *which* items each strategy picks.
    """
    if times is None:
        times = decision_times(market, horizon_days=params.horizon_days)
    results = {name: StrategyResult(name) for name in strategies}
    for ts in times:
        snaps = snapshots_by_time[ts] if snapshots_by_time is not None else snapshots_at(market, ts)
        if not snaps:
            continue
        by_id = {s.variant_id: s for s in snaps}
        for name, scorer in strategies.items():
            scores = scorer(snaps)
            ranked = sorted(
                (vid for vid, sc in scores.items() if sc is not None and sc > 0 and vid in by_id),
                key=lambda vid: -scores[vid],
            )[:top_k]
            if not ranked:
                continue
            outcomes = []
            for vid in ranked:
                snap = by_id[vid]
                ask = estimate(snap, params).ask_price
                outcomes.append(
                    realize_trade(market.variants[vid], snap, ask=ask, horizon_days=params.horizon_days, fee_pct=params.fee_pct)
                )
            results[name].trades.extend(outcomes)
            results[name].per_decision[ts] = sum(o.ret for o in outcomes) / sum(o.days for o in outcomes)
    return results
