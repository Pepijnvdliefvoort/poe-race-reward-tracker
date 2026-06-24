"""
Revert inferred sales recorded during a bad poll (e.g. fingerprint churn after deploy).

Marks matching rows in `sales` as reverted and rebuilds `item_polls` sale counters for
affected variants.

Usage on VPS (from repo root /opt/poe-market-flips):

  # Preview
  .venv/bin/python scripts/revert_false_sales_poll.py --dry-run

  # Revert the Jun 2026 fingerprint-churn poll (cycle 198228, poll_run id 1771)
  .venv/bin/python scripts/revert_false_sales_poll.py --poll-run-id 1771

  # Or locate by cycle number
  .venv/bin/python scripts/revert_false_sales_poll.py --cycle-number 198228

  # Auto-pick the poll run with the most active sales on a given UTC date
  .venv/bin/python scripts/revert_false_sales_poll.py --auto-date 2026-06-24 --dry-run

Stop the poller first if you can (systemctl stop poe-market-poller); the server can stay up.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from storage.db import Database

SALE_RULES = ("confirmed_transfer", "likely_instant_sale", "likely_non_instant_online_sale")
DEFAULT_REASON = "fingerprint_churn_false_positive"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_poll_run(
    con: sqlite3.Connection,
    *,
    poll_run_id: int | None,
    cycle_number: int | None,
    auto_date: str | None,
) -> sqlite3.Row:
    if poll_run_id is not None:
        row = con.execute(
            "SELECT id, cycle_number, started_at_utc FROM poll_runs WHERE id = ?",
            (int(poll_run_id),),
        ).fetchone()
        if not row:
            raise SystemExit(f"No poll_run with id={poll_run_id}")
        return row

    if cycle_number is not None:
        row = con.execute(
            "SELECT id, cycle_number, started_at_utc FROM poll_runs WHERE cycle_number = ?",
            (int(cycle_number),),
        ).fetchone()
        if not row:
            raise SystemExit(f"No poll_run with cycle_number={cycle_number}")
        return row

    if auto_date:
        row = con.execute(
            """
            SELECT pr.id, pr.cycle_number, pr.started_at_utc, COUNT(*) AS sale_cnt
            FROM sales s
            JOIN item_polls ip ON ip.id = s.item_poll_id
            JOIN poll_runs pr ON pr.id = ip.poll_run_id
            WHERE s.occurred_at_utc LIKE ? || '%'
              AND s.reverted_at_utc IS NULL
              AND s.rule IN ('confirmed_transfer', 'likely_instant_sale', 'likely_non_instant_online_sale')
            GROUP BY pr.id
            ORDER BY sale_cnt DESC
            LIMIT 1
            """,
            (str(auto_date).strip(),),
        ).fetchone()
        if not row:
            raise SystemExit(f"No active sales found on date {auto_date!r}")
        return row

    raise SystemExit("Specify --poll-run-id, --cycle-number, or --auto-date")


def _count_sales_for_poll(con: sqlite3.Connection, poll_run_id: int) -> dict[str, int]:
    row = con.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN s.rule = 'likely_instant_sale' THEN 1 ELSE 0 END) AS instant,
          SUM(CASE WHEN s.rule = 'likely_non_instant_online_sale' THEN 1 ELSE 0 END) AS non_inst,
          SUM(CASE WHEN s.rule = 'confirmed_transfer' THEN 1 ELSE 0 END) AS xfer,
          COUNT(DISTINCT s.item_variant_id) AS variants
        FROM sales s
        JOIN item_polls ip ON ip.id = s.item_poll_id
        WHERE ip.poll_run_id = ?
          AND s.reverted_at_utc IS NULL
          AND s.rule IN ('confirmed_transfer', 'likely_instant_sale', 'likely_non_instant_online_sale')
        """,
        (int(poll_run_id),),
    ).fetchone()
    return {
        "total": int(row[0] or 0),
        "instant": int(row[1] or 0),
        "non_inst": int(row[2] or 0),
        "xfer": int(row[3] or 0),
        "variants": int(row[4] or 0),
    }


def _recalculate_variant_sale_counters(con: sqlite3.Connection, variant_id: int) -> int:
    con.execute(
        """
        UPDATE item_polls
        SET
          inf_confirmed_transfer = 0,
          inf_likely_instant_sale = 0,
          inf_likely_non_instant_online = 0
        WHERE item_variant_id = ?
        """,
        (int(variant_id),),
    )
    rollups = con.execute(
        """
        SELECT
          item_poll_id,
          SUM(CASE WHEN rule = 'confirmed_transfer' THEN COALESCE(quantity, 1) ELSE 0 END) AS c_xfer,
          SUM(CASE WHEN rule = 'likely_instant_sale' THEN COALESCE(quantity, 1) ELSE 0 END) AS c_inst,
          SUM(CASE WHEN rule = 'likely_non_instant_online_sale' THEN COALESCE(quantity, 1) ELSE 0 END) AS c_non_inst
        FROM sales
        WHERE item_variant_id = ?
          AND reverted_at_utc IS NULL
          AND rule IN ('confirmed_transfer', 'likely_instant_sale', 'likely_non_instant_online_sale')
        GROUP BY item_poll_id
        """,
        (int(variant_id),),
    ).fetchall()
    for r in rollups:
        con.execute(
            """
            UPDATE item_polls
            SET
              inf_confirmed_transfer = ?,
              inf_likely_instant_sale = ?,
              inf_likely_non_instant_online = ?
            WHERE id = ?
              AND item_variant_id = ?
            """,
            (
                int(r[1] or 0),
                int(r[2] or 0),
                int(r[3] or 0),
                int(r[0]),
                int(variant_id),
            ),
        )
    return int(
        con.execute(
            "SELECT COUNT(*) FROM item_polls WHERE item_variant_id = ?",
            (int(variant_id),),
        ).fetchone()[0]
        or 0
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Revert false inferred sales from one poll run.")
    parser.add_argument("--db", default=None, help="Path to market.db (default: data/market.db)")
    parser.add_argument("--poll-run-id", type=int, default=None, help="poll_runs.id to revert (e.g. 1771)")
    parser.add_argument("--cycle-number", type=int, default=None, help="poll_runs.cycle_number (e.g. 198228)")
    parser.add_argument(
        "--auto-date",
        default=None,
        help="UTC date YYYY-MM-DD; pick the poll run with the most active sales that day",
    )
    parser.add_argument("--reason", default=DEFAULT_REASON, help="sales.reverted_reason value")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only; do not write")
    args = parser.parse_args()

    if sum(x is not None for x in (args.poll_run_id, args.cycle_number, args.auto_date)) != 1:
        parser.error("Specify exactly one of --poll-run-id, --cycle-number, or --auto-date")

    db_path = Path(args.db) if args.db else Database(ROOT_DIR).path
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        poll = _resolve_poll_run(
            con,
            poll_run_id=args.poll_run_id,
            cycle_number=args.cycle_number,
            auto_date=args.auto_date,
        )
        poll_run_id = int(poll["id"])
        counts = _count_sales_for_poll(con, poll_run_id)

        print(f"Database: {db_path}")
        print(
            f"Poll run id={poll_run_id} cycle={poll['cycle_number']} "
            f"started={poll['started_at_utc']}"
        )
        print(
            f"Active sales to revert: {counts['total']} "
            f"(instant={counts['instant']} non_inst={counts['non_inst']} "
            f"xfer={counts['xfer']}, variants={counts['variants']})"
        )
        if counts["total"] == 0:
            print("Nothing to do.")
            return

        if args.dry_run:
            print("Dry run — no changes written.")
            return

        reverted_at = _utc_now_iso()
        cur = con.execute(
            """
            UPDATE sales
            SET
              reverted_at_utc = ?,
              reverted_by_item_poll_id = item_poll_id,
              reverted_reason = ?
            WHERE id IN (
              SELECT s.id
              FROM sales s
              JOIN item_polls ip ON ip.id = s.item_poll_id
              WHERE ip.poll_run_id = ?
                AND s.reverted_at_utc IS NULL
                AND s.rule IN ('confirmed_transfer', 'likely_instant_sale', 'likely_non_instant_online_sale')
            )
            """,
            (reverted_at, str(args.reason), poll_run_id),
        )
        reverted = int(cur.rowcount or 0)

        variant_ids = [
            int(r[0])
            for r in con.execute(
                """
                SELECT DISTINCT s.item_variant_id
                FROM sales s
                JOIN item_polls ip ON ip.id = s.item_poll_id
                WHERE ip.poll_run_id = ?
                """,
                (poll_run_id,),
            ).fetchall()
        ]
        polls_touched = 0
        for vid in variant_ids:
            polls_touched += _recalculate_variant_sale_counters(con, vid)

        con.commit()
        print(f"Reverted {reverted} sale row(s).")
        print(f"Recalculated sale counters for {len(variant_ids)} variant(s) ({polls_touched} item_poll rows).")
        print("Done.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
