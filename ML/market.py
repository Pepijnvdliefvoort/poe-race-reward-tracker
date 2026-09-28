from __future__ import annotations

import math
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Sales recorded far above the cheapest listing of the same poll are not credible executions. The
# main case: "1 mirror" anchor listings on items trading around 0.4 mirror vanish and get inferred
# as sales (about 1 in 5 recorded sales, Sep 2026). Dropping them can only remove evidence of high
# sale prices, never add it.
MAX_SALE_TO_FLOOR_RATIO = 1.5

_INSTANT_FLOORS_SQL = """
SELECT ls.item_poll_id,
       MIN(CASE WHEN lower(ls.currency) IN ('mirror', 'mirrors', 'mirror of kalandra') THEN ls.amount
                WHEN lower(ls.currency) IN ('divine', 'divines', 'div', 'divine orb', 'divine orbs')
                     AND pr.divines_per_mirror > 0 THEN ls.amount / pr.divines_per_mirror
           END) AS instant_floor
FROM listing_snapshots ls
JOIN item_polls ip ON ip.id = ls.item_poll_id
JOIN poll_runs pr ON pr.id = ip.poll_run_id
WHERE ls.is_instant_buyout = 1 AND ls.amount > 0
GROUP BY ls.item_poll_id
"""


def parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def positive_or_none(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


@dataclass
class PollPoint:
    ts: float  # epoch seconds (UTC)
    floor_mirror: float | None  # cheapest mirror-equivalent listing in this poll (any listing type)
    total_results: int
    new_listing_rows: int
    instant_floor: float | None = None  # cheapest instant-buyout listing: what you can actually buy


@dataclass
class SalePoint:
    ts: float
    price_mirror: float


@dataclass
class VariantHistory:
    variant_id: int
    base_item_name: str
    display_name: str
    mode: str
    polls: list[PollPoint] = field(default_factory=list)
    sales: list[SalePoint] = field(default_factory=list)
    _poll_ts: list[float] = field(default_factory=list, repr=False)
    _sale_ts: list[float] = field(default_factory=list, repr=False)

    def finalize(self) -> None:
        self.polls.sort(key=lambda p: p.ts)
        self.sales.sort(key=lambda s: s.ts)
        self._poll_ts = [p.ts for p in self.polls]
        self._sale_ts = [s.ts for s in self.sales]

    def polls_upto(self, ts: float) -> int:
        """Number of polls with timestamp <= ts."""
        return bisect_right(self._poll_ts, ts)

    def sales_upto(self, ts: float) -> int:
        """Number of sales with timestamp <= ts."""
        return bisect_right(self._sale_ts, ts)


@dataclass
class Market:
    variants: dict[int, VariantHistory]
    start_ts: float | None
    end_ts: float | None
    sales_dropped_implausible: int = 0


def load_market(con: sqlite3.Connection) -> Market:
    """
    Load every tracked variant's poll history and non-reverted sales.

    Sales without a mirror-equivalent price are skipped: they can't be valued, and both the
    estimator and the simulation work in mirror terms.
    """
    con.row_factory = sqlite3.Row
    variants: dict[int, VariantHistory] = {}
    for r in con.execute(
        """
        SELECT v.id, i.name AS base_item_name, v.display_name, v.mode
        FROM item_variants v JOIN items i ON i.id = v.item_id
        """
    ):
        variants[int(r["id"])] = VariantHistory(
            variant_id=int(r["id"]),
            base_item_name=str(r["base_item_name"] or ""),
            display_name=str(r["display_name"] or ""),
            mode=str(r["mode"] or ""),
        )

    instant_floors = {int(r[0]): positive_or_none(r[1]) for r in con.execute(_INSTANT_FLOORS_SQL)}

    start_ts: float | None = None
    end_ts: float | None = None
    for r in con.execute(
        """
        SELECT id, item_variant_id, requested_at_utc, lowest_mirror, total_results, inf_new_listing_rows
        FROM item_polls
        """
    ):
        hist = variants.get(int(r["item_variant_id"]))
        dt = parse_utc(r["requested_at_utc"])
        if hist is None or dt is None:
            continue
        ts = dt.timestamp()
        hist.polls.append(
            PollPoint(
                ts=ts,
                floor_mirror=positive_or_none(r["lowest_mirror"]),
                total_results=max(0, int(r["total_results"] or 0)),
                new_listing_rows=max(0, int(r["inf_new_listing_rows"] or 0)),
                instant_floor=instant_floors.get(int(r["id"])),
            )
        )
        start_ts = ts if start_ts is None else min(start_ts, ts)
        end_ts = ts if end_ts is None else max(end_ts, ts)

    dropped = 0
    for r in con.execute(
        """
        SELECT s.item_variant_id, s.occurred_at_utc, s.mirror_equiv, ip.lowest_mirror AS poll_floor
        FROM sales s
        LEFT JOIN item_polls ip ON ip.id = s.item_poll_id
        WHERE s.reverted_at_utc IS NULL AND s.mirror_equiv IS NOT NULL AND s.mirror_equiv > 0
        """
    ):
        hist = variants.get(int(r["item_variant_id"]))
        dt = parse_utc(r["occurred_at_utc"])
        price = positive_or_none(r["mirror_equiv"])
        if hist is None or dt is None or price is None:
            continue
        poll_floor = positive_or_none(r["poll_floor"])
        if poll_floor is not None and price > poll_floor * MAX_SALE_TO_FLOOR_RATIO:
            dropped += 1
            continue
        hist.sales.append(SalePoint(ts=dt.timestamp(), price_mirror=price))

    for hist in variants.values():
        hist.finalize()
    return Market(variants=variants, start_ts=start_ts, end_ts=end_ts, sales_dropped_implausible=dropped)
