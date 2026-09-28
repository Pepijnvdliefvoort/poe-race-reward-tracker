from __future__ import annotations

import math
import sqlite3
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import groupby
from typing import Any, Iterator

# Legacy blunt filter (kept for comparison): drop sales above this multiple of the poll's cheapest
# listing of any roll.
MAX_SALE_TO_FLOOR_RATIO = 1.5
# Roll-aware filter (default): a recorded sale is not credible if, in the poll just before it,
# another seller listed the same roll (fingerprint) at least this much cheaper. A buyer would have
# taken that copy; typical case is a "1 mirror" anchor listing vanishing while the same roll sits
# at 0.4 mirror elsewhere (about 60% of the 1-mirror "sales" in the Sep 2026 production DB).
CHEAPER_SAME_ROLL_MARGIN = 0.10
SALE_FILTERS = ("roll", "floor_ratio", "none")

LADDER_DEPTH = 15  # cheapest instant listings kept per poll (enough to count the queue ahead of us)

_MIRROR = {"mirror", "mirrors", "mirror of kalandra"}
_DIVINE = {"divine", "divines", "div", "divine orb", "divine orbs"}


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


def mirror_equivalent(amount: Any, currency: Any, divines_per_mirror: float | None) -> float | None:
    a = positive_or_none(amount)
    if a is None:
        return None
    c = str(currency or "").strip().lower()
    if c in _MIRROR:
        return a
    if c in _DIVINE and divines_per_mirror:
        return a / divines_per_mirror
    return None


@dataclass
class PollPoint:
    ts: float  # epoch seconds (UTC)
    floor_mirror: float | None  # cheapest mirror-equivalent listing in this poll (any listing type)
    total_results: int
    new_listing_rows: int
    instant_ladder: array = field(default_factory=lambda: array("f"))  # cheapest instant listings, ascending

    @property
    def instant_floor(self) -> float | None:
        """Cheapest instant-buyout listing: what you can actually buy."""
        return float(self.instant_ladder[0]) if self.instant_ladder else None


@dataclass
class SalePoint:
    ts: float
    price_mirror: float


@dataclass(frozen=True)
class ListingEpisode:
    """One listing (seller + roll) at one price, from first seen until it vanished or repriced."""

    start_ts: float
    end_ts: float  # poll where it was gone (or last seen, when still listed at the end of the data)
    price_mirror: float
    instant: bool
    sold: bool  # vanished together with a credible recorded sale of the same seller + roll
    still_listed: bool


@dataclass
class VariantHistory:
    variant_id: int
    base_item_name: str
    display_name: str
    mode: str
    polls: list[PollPoint] = field(default_factory=list)
    sales: list[SalePoint] = field(default_factory=list)
    episodes: list[ListingEpisode] = field(default_factory=list)
    _poll_ts: list[float] = field(default_factory=list, repr=False)
    _sale_ts: list[float] = field(default_factory=list, repr=False)

    def finalize(self) -> None:
        self.polls.sort(key=lambda p: p.ts)
        self.sales.sort(key=lambda s: s.ts)
        self.episodes.sort(key=lambda e: e.start_ts)
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
    sales_kept: int = 0
    sale_filter: str = "roll"


_LISTING_COLUMNS = """ip.item_variant_id, ls.item_poll_id, ls.seller_name, ls.fingerprint, ls.amount, ls.currency,
               ls.is_instant_buyout, pr.divines_per_mirror"""
RECENT_LADDER_DAYS = 7  # light mode: listings kept for the last week of polls (buy price + queue)


def _listings_for_polls(con: sqlite3.Connection, poll_ids: set[int]) -> dict[int, list[tuple]]:
    out: dict[int, list[tuple]] = {}
    ids = sorted(poll_ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        for row in con.execute(
            f"""SELECT {_LISTING_COLUMNS}
                FROM listing_snapshots ls
                JOIN item_polls ip ON ip.id = ls.item_poll_id
                JOIN poll_runs pr ON pr.id = ip.poll_run_id
                WHERE ls.item_poll_id IN ({",".join("?" for _ in chunk)})
                ORDER BY ls.item_poll_id, ls.rank""",
            chunk,
        ):
            out.setdefault(int(row[1]), []).append(tuple(row))
    return out


def _listing_stream(con: sqlite3.Connection, since_iso: str | None) -> Iterator[tuple]:
    where = "WHERE ip.requested_at_utc >= ?" if since_iso else ""
    args = (since_iso,) if since_iso else ()
    return con.execute(
        f"""
        SELECT {_LISTING_COLUMNS}
        FROM listing_snapshots ls
        JOIN item_polls ip ON ip.id = ls.item_poll_id
        JOIN poll_runs pr ON pr.id = ip.poll_run_id
        {where}
        ORDER BY ip.item_variant_id, ls.item_poll_id, ls.rank
        """,
        args,
    )


def load_market(
    con: sqlite3.Connection,
    *,
    sale_filter: str = "roll",
    since_ts: float | None = None,
    episodes: bool = True,
) -> Market:
    """
    Load every tracked variant's polls, listing episodes and credible non-reverted sales.

    `since_ts` limits history. `episodes=False` is the light mode for the server: it only reads
    listings for the last week of polls (buy price and queue) and for the polls just before each
    sale (credibility check), and builds no listing episodes. Sales without a mirror-equivalent
    price are skipped: everything downstream works in mirror terms.
    """
    if sale_filter not in SALE_FILTERS:
        raise ValueError(f"sale_filter must be one of {SALE_FILTERS}")
    con.row_factory = sqlite3.Row
    since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat() if since_ts is not None else None

    variants: dict[int, VariantHistory] = {}
    for r in con.execute(
        "SELECT v.id, i.name AS base_item_name, v.display_name, v.mode FROM item_variants v JOIN items i ON i.id = v.item_id"
    ):
        variants[int(r["id"])] = VariantHistory(
            variant_id=int(r["id"]),
            base_item_name=str(r["base_item_name"] or ""),
            display_name=str(r["display_name"] or ""),
            mode=str(r["mode"] or ""),
        )

    # Polls per variant, in time order (ids increase with time).
    poll_rows: dict[int, list[tuple[int, PollPoint]]] = {}
    start_ts: float | None = None
    end_ts: float | None = None
    where = "WHERE requested_at_utc >= ?" if since_iso else ""
    for r in con.execute(
        f"""SELECT id, item_variant_id, requested_at_utc, lowest_mirror, total_results, inf_new_listing_rows
            FROM item_polls {where} ORDER BY item_variant_id, id""",
        (since_iso,) if since_iso else (),
    ):
        vid = int(r["item_variant_id"])
        dt = parse_utc(r["requested_at_utc"])
        if vid not in variants or dt is None:
            continue
        ts = dt.timestamp()
        point = PollPoint(
            ts=ts,
            floor_mirror=positive_or_none(r["lowest_mirror"]),
            total_results=max(0, int(r["total_results"] or 0)),
            new_listing_rows=max(0, int(r["inf_new_listing_rows"] or 0)),
        )
        poll_rows.setdefault(vid, []).append((int(r["id"]), point))
        start_ts = ts if start_ts is None else min(start_ts, ts)
        end_ts = ts if end_ts is None else max(end_ts, ts)

    # Sales keyed by the poll in which they were observed.
    sales_by_poll: dict[int, list[sqlite3.Row]] = {}
    where = "AND s.occurred_at_utc >= ?" if since_iso else ""
    for r in con.execute(
        f"""
        SELECT s.item_variant_id, s.item_poll_id, s.occurred_at_utc, s.mirror_equiv, s.fingerprint, s.seller,
               ip.lowest_mirror AS poll_floor
        FROM sales s LEFT JOIN item_polls ip ON ip.id = s.item_poll_id
        WHERE s.reverted_at_utc IS NULL AND s.mirror_equiv IS NOT NULL AND s.mirror_equiv > 0 {where}
        """,
        (since_iso,) if since_iso else (),
    ):
        sales_by_poll.setdefault(int(r["item_poll_id"]), []).append(r)

    light_rows: dict[int, list[tuple]] = {}
    if not episodes:
        needed: set[int] = set()
        recent = (end_ts or 0.0) - RECENT_LADDER_DAYS * 86400.0
        for rows in poll_rows.values():
            ids = [pid for pid, _ in rows]
            needed.update(pid for pid, pt in rows if pt.ts >= recent)
            index = {pid: i for i, pid in enumerate(ids)}
            needed.update(ids[index[pid] - 1] for pid in ids if pid in sales_by_poll and index[pid] > 0)
        light_rows = _listings_for_polls(con, needed)

    # One pass over the listings: per-poll instant ladder, listing episodes, sale credibility.
    # Note: advancing a groupby invalidates its current group, so the outer stream only moves on
    # after a variant's listings have been fully consumed.
    stream = groupby(_listing_stream(con, since_iso), key=lambda row: int(row[0])) if episodes else iter(())
    dropped = kept = 0
    current = next(stream, None)

    for vid in sorted(poll_rows):
        while current is not None and current[0] < vid:
            current = next(stream, None)
        has_listings = current is not None and current[0] == vid
        per_poll = groupby(current[1], key=lambda row: int(row[1])) if has_listings else iter(())

        hist = variants[vid]
        pending = next(per_poll, None)
        active: dict[tuple[str, str], list] = {}  # key -> [start_ts, last_ts, price, instant]
        prev_listings: list[tuple[str, str, float | None, bool]] = []

        for poll_id, point in poll_rows[vid]:
            raw_rows: list[tuple] = []
            if not episodes:
                raw_rows = light_rows.get(poll_id, [])
            elif pending is not None and pending[0] == poll_id:
                raw_rows = list(pending[1])
                pending = next(per_poll, None)
            while pending is not None and pending[0] < poll_id:  # listings for polls outside the window
                pending = next(per_poll, None)
            listings = [
                (str(row[2] or ""), str(row[3] or ""), mirror_equivalent(row[4], row[5], positive_or_none(row[7])), bool(row[6]))
                for row in raw_rows
            ]

            point.instant_ladder = array("f", sorted(p for _, _, p, inst in listings if inst and p is not None)[:LADDER_DEPTH])
            hist.polls.append(point)

            credible_keys: set[tuple[str, str]] = set()
            for s in sales_by_poll.get(poll_id, []):
                price = positive_or_none(s["mirror_equiv"])
                dt = parse_utc(s["occurred_at_utc"])
                if price is None or dt is None:
                    continue
                if not _sale_is_credible(s, price, prev_listings, sale_filter):
                    dropped += 1
                    continue
                kept += 1
                hist.sales.append(SalePoint(ts=dt.timestamp(), price_mirror=price))
                credible_keys.add((str(s["seller"] or ""), str(s["fingerprint"] or "")))

            if not episodes:
                prev_listings = listings
                continue
            seen: dict[tuple[str, str], tuple[float, bool]] = {}
            for seller, fp, price, inst in listings:
                if price is not None and (seller, fp) not in seen:
                    seen[(seller, fp)] = (price, inst)
            for key, ep in list(active.items()):
                now = seen.get(key)
                if now is not None and abs(now[0] - ep[2]) <= 1e-6 * max(1.0, ep[2]):
                    ep[1] = point.ts
                    continue
                # Vanished (possibly sold) or repriced: close this episode at this poll.
                hist.episodes.append(
                    ListingEpisode(ep[0], point.ts, ep[2], ep[3], sold=now is None and key in credible_keys, still_listed=False)
                )
                del active[key]
            for key, (price, inst) in seen.items():
                if key not in active:
                    active[key] = [point.ts, point.ts, price, inst]
            prev_listings = listings

        for ep in active.values():  # empty in light mode
            hist.episodes.append(ListingEpisode(ep[0], ep[1], ep[2], ep[3], sold=False, still_listed=True))
        if has_listings:
            current = next(stream, None)

    # Variants with sales but no polls in the window (rare): nothing to rank, sales are irrelevant.
    for hist in variants.values():
        hist.finalize()
    return Market(
        variants=variants,
        start_ts=start_ts,
        end_ts=end_ts,
        sales_dropped_implausible=dropped,
        sales_kept=kept,
        sale_filter=sale_filter,
    )


def _sale_is_credible(sale: sqlite3.Row, price: float, prev_listings: list, sale_filter: str) -> bool:
    if sale_filter == "none":
        return True
    if sale_filter == "floor_ratio":
        floor = positive_or_none(sale["poll_floor"])
        return floor is None or price <= floor * MAX_SALE_TO_FLOOR_RATIO
    fp = str(sale["fingerprint"] or "")
    seller = str(sale["seller"] or "")
    if not fp:
        return True
    cutoff = price * (1.0 - CHEAPER_SAME_ROLL_MARGIN)
    return not any(
        lf == fp and ls != seller and lp is not None and lp < cutoff for ls, lf, lp, _inst in prev_listings
    )
