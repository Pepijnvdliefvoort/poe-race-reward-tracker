"""
Retroactive clean-up of inferred sales that were not real sales.

Applies the same rules the poller now uses for new polls (see poller/sale_inference_engine.py rules
8 and 9, and the transfer ping-pong check in StorageService.write_poll_result) to sales that were
recorded before those rules existed:

- mass_vanish: at least MASS_VANISH_MIN_SELLERS sellers, and at least half of the previous poll's
  sellers, "sold" in the same poll (listings vanished en masse).
- seller_burst: more than one sale from the same seller in the same poll (a stack pulled at once);
  the first one is kept.
- transfer_ping_pong: confirmed transfers between two sellers who "transferred" to each other more
  than once.
- anomaly_day: every remaining sale on a UTC day whose sale count is more than ANOMALY_DAY_MULTIPLE x
  the median of the previous ANOMALY_LOOKBACK_DAYS days (and at least ANOMALY_MIN_SALES). Mass vanishes
  also come as one seller per poll spread over a day (2026-07-21..24), which no per-poll rule sees.
  Same rule as the ML loader (ML/market.py). The poller also runs this for the current day after
  every cycle (`revert_anomaly_day_sales`).

Rows are marked reverted (reverted_reason), never deleted, and per-poll counters are rebuilt.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from poller.sale_inference_engine import MASS_VANISH_MIN_SELLER_SHARE, MASS_VANISH_MIN_SELLERS

VANISH_SALE_RULES = ("likely_instant_sale", "likely_non_instant_online_sale")
SALE_RULES = ("confirmed_transfer", *VANISH_SALE_RULES)
ANOMALY_DAY_MULTIPLE = 4.0
ANOMALY_MIN_SALES = 30
ANOMALY_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class PlannedRevert:
    sale_id: int
    item_variant_id: int
    reason: str


def plan_reverts(con: sqlite3.Connection) -> list[PlannedRevert]:
    """Active sales that the current rules would not have counted."""
    planned: list[PlannedRevert] = []
    taken: set[int] = set()

    by_poll: dict[int, list[tuple[int, int, str]]] = {}
    for sale_id, poll_id, variant_id, seller in con.execute(
        f"""
        SELECT id, item_poll_id, item_variant_id, seller FROM sales
        WHERE reverted_at_utc IS NULL AND rule IN ({",".join("?" for _ in VANISH_SALE_RULES)})
        ORDER BY id
        """,
        VANISH_SALE_RULES,
    ):
        by_poll.setdefault(int(poll_id), []).append((int(sale_id), int(variant_id), str(seller or "")))

    for poll_id, sales in by_poll.items():
        variant_id = sales[0][1]
        sellers = {seller.casefold() for _, _, seller in sales}
        if len(sellers) >= MASS_VANISH_MIN_SELLERS:
            prev = con.execute(
                "SELECT id FROM item_polls WHERE item_variant_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                (variant_id, poll_id),
            ).fetchone()
            prev_sellers = 0
            if prev is not None:
                prev_sellers = int(
                    con.execute(
                        "SELECT COUNT(DISTINCT seller_name) FROM listing_snapshots WHERE item_poll_id = ?",
                        (int(prev[0]),),
                    ).fetchone()[0]
                    or 0
                )
            if len(sellers) >= MASS_VANISH_MIN_SELLER_SHARE * max(1, prev_sellers):
                for sale_id, vid, _ in sales:
                    planned.append(PlannedRevert(sale_id, vid, "mass_vanish"))
                    taken.add(sale_id)
                continue
        seen: set[str] = set()
        for sale_id, vid, seller in sales:
            key = seller.casefold()
            if key in seen:
                planned.append(PlannedRevert(sale_id, vid, "seller_burst"))
                taken.add(sale_id)
            seen.add(key)

    pairs: dict[tuple[int, frozenset], list[int]] = {}
    for sale_id, variant_id, seller, buyer in con.execute(
        """
        SELECT id, item_variant_id, seller, buyer FROM sales
        WHERE reverted_at_utc IS NULL AND rule = 'confirmed_transfer' ORDER BY id
        """
    ):
        a, b = str(seller or "").strip(), str(buyer or "").strip()
        if a and b:
            pairs.setdefault((int(variant_id), frozenset((a, b))), []).append(int(sale_id))
    for (variant_id, _pair), ids in pairs.items():
        if len(ids) >= 2:
            for i in ids:
                if i not in taken:
                    planned.append(PlannedRevert(i, variant_id, "transfer_ping_pong"))
                    taken.add(i)

    planned.extend(plan_anomaly_day_reverts(con, exclude=taken))
    return planned


def _active_sales_by_day(con: sqlite3.Connection, exclude: set[int]) -> dict[str, list[tuple[int, int]]]:
    by_day: dict[str, list[tuple[int, int]]] = {}
    for sale_id, variant_id, day in con.execute(
        f"""
        SELECT id, item_variant_id, substr(occurred_at_utc, 1, 10) FROM sales
        WHERE reverted_at_utc IS NULL AND rule IN ({",".join("?" for _ in SALE_RULES)})
        """,
        SALE_RULES,
    ):
        if int(sale_id) not in exclude:
            by_day.setdefault(str(day), []).append((int(sale_id), int(variant_id)))
    return by_day


def plan_anomaly_day_reverts(
    con: sqlite3.Connection, *, exclude: set[int] | None = None, only_days: set[str] | None = None
) -> list[PlannedRevert]:
    """Sales on days whose count spikes far above the preceding two weeks (see module docstring)."""
    by_day = _active_sales_by_day(con, exclude or set())
    days = sorted(by_day)
    planned: list[PlannedRevert] = []
    for i, day in enumerate(days):
        if only_days is not None and day not in only_days:
            continue
        n = len(by_day[day])
        history = [len(by_day[d]) for d in days[max(0, i - ANOMALY_LOOKBACK_DAYS):i]]
        if not history or n < ANOMALY_MIN_SALES:
            continue
        baseline = sorted(history)[len(history) // 2]
        if n > ANOMALY_DAY_MULTIPLE * max(1, baseline):
            planned.extend(PlannedRevert(sid, vid, "anomaly_day") for sid, vid in by_day[day])
    return planned


def revert_anomaly_day_sales(con: sqlite3.Connection, day: str) -> list[PlannedRevert]:
    """Poller hook: revert the given UTC day's sales if that day has become an anomaly day (commits)."""
    planned = plan_anomaly_day_reverts(con, only_days={day})
    if planned:
        apply_reverts(con, planned)
        con.commit()
    return planned


def rebuild_variant_sale_counters(con: sqlite3.Connection, variant_id: int) -> None:
    """Set each poll's sale counters to its active sales."""
    con.execute(
        """
        UPDATE item_polls SET inf_confirmed_transfer = 0, inf_likely_instant_sale = 0, inf_likely_non_instant_online = 0
        WHERE item_variant_id = ?
        """,
        (int(variant_id),),
    )
    for poll_id, xfer, inst, online in con.execute(
        """
        SELECT item_poll_id,
          SUM(CASE WHEN rule = 'confirmed_transfer' THEN COALESCE(quantity, 1) ELSE 0 END),
          SUM(CASE WHEN rule = 'likely_instant_sale' THEN COALESCE(quantity, 1) ELSE 0 END),
          SUM(CASE WHEN rule = 'likely_non_instant_online_sale' THEN COALESCE(quantity, 1) ELSE 0 END)
        FROM sales
        WHERE item_variant_id = ? AND reverted_at_utc IS NULL
        GROUP BY item_poll_id
        """,
        (int(variant_id),),
    ).fetchall():
        con.execute(
            """
            UPDATE item_polls SET inf_confirmed_transfer = ?, inf_likely_instant_sale = ?, inf_likely_non_instant_online = ?
            WHERE id = ?
            """,
            (int(xfer or 0), int(inst or 0), int(online or 0), int(poll_id)),
        )


def apply_reverts(con: sqlite3.Connection, planned: list[PlannedRevert]) -> None:
    """Mark the planned sales reverted and rebuild counters for the affected variants (no commit)."""
    now = datetime.now(timezone.utc).isoformat()
    for p in planned:
        con.execute(
            "UPDATE sales SET reverted_at_utc = ?, reverted_reason = ? WHERE id = ? AND reverted_at_utc IS NULL",
            (now, p.reason, int(p.sale_id)),
        )
    for variant_id in sorted({p.item_variant_id for p in planned}):
        rebuild_variant_sale_counters(con, variant_id)
