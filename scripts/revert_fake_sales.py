"""
Revert recorded sales that were not real sales: mass vanishes, one seller's stack pulled at once,
transfer ping-pong between the same two sellers, and anomaly days (see storage/sale_hygiene.py).

New polls already apply these rules; this cleans up history recorded before them. Rows are marked
reverted with a reason, never deleted.

Usage on VPS (from repo root /opt/poe-market-flips):

  # Preview (default)
  .venv/bin/python scripts/revert_fake_sales.py

  # Apply
  .venv/bin/python scripts/revert_fake_sales.py --apply

Stop the poller first if you can (systemctl stop poe-market-poller); the server can stay up.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from storage.db import Database
from storage.sale_hygiene import apply_reverts, plan_reverts


def main() -> None:
    parser = argparse.ArgumentParser(description="Revert inferred sales that were not real sales.")
    parser.add_argument("--db", default=None, help="Path to market.db (default: data/market.db)")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: preview only)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else Database(ROOT_DIR).path
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    con = sqlite3.connect(db_path)
    try:
        active = int(
            con.execute(
                """SELECT COUNT(*) FROM sales WHERE reverted_at_utc IS NULL
                   AND rule IN ('confirmed_transfer', 'likely_instant_sale', 'likely_non_instant_online_sale')"""
            ).fetchone()[0]
        )
        planned = plan_reverts(con)
        by_reason = Counter(p.reason for p in planned)
        print(f"Active sales: {active}")
        for reason in ("mass_vanish", "seller_burst", "transfer_ping_pong", "anomaly_day"):
            print(f"  {reason:<20} {by_reason.get(reason, 0):>6}")
        print(f"Would remain: {active - len(planned)} ({len({p.item_variant_id for p in planned})} variants affected)")
        if not args.apply:
            print("Preview only. Run with --apply to write.")
            return
        apply_reverts(con, planned)
        con.commit()
        print(f"Reverted {len(planned)} sales.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
