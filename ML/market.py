from __future__ import annotations

import math
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


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
    floor_mirror: float | None  # cheapest mirror-equivalent listing in this poll
    total_results: int
    new_listing_rows: int


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

    start_ts: float | None = None
    end_ts: float | None = None
    for r in con.execute(
        """
        SELECT item_variant_id, requested_at_utc, lowest_mirror, total_results, inf_new_listing_rows
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
            )
        )
        start_ts = ts if start_ts is None else min(start_ts, ts)
        end_ts = ts if end_ts is None else max(end_ts, ts)

    for r in con.execute(
        """
        SELECT item_variant_id, occurred_at_utc, mirror_equiv
        FROM sales
        WHERE reverted_at_utc IS NULL AND mirror_equiv IS NOT NULL AND mirror_equiv > 0
        """
    ):
        hist = variants.get(int(r["item_variant_id"]))
        dt = parse_utc(r["occurred_at_utc"])
        price = positive_or_none(r["mirror_equiv"])
        if hist is None or dt is None or price is None:
            continue
        hist.sales.append(SalePoint(ts=dt.timestamp(), price_mirror=price))

    for hist in variants.values():
        hist.finalize()
    return Market(variants=variants, start_ts=start_ts, end_ts=end_ts)
