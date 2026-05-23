"""
One-off: fix item_polls where relist_same_seller undid a sale but reconcile stored 0 instead of -1.

Usage (from repo root):
  python scripts/backfill_relist_poll_counters.py
  python scripts/backfill_relist_poll_counters.py --db data/market.db --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from storage.db import Database


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="Path to market.db (default: data/market.db)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else Database(ROOT_DIR).path
    con = sqlite3.connect(db_path)
    try:
        instant_ids = [
            r[0]
            for r in con.execute(
                """
                SELECT DISTINCT ip.id
                FROM item_polls ip
                JOIN inference_events ie ON ie.item_poll_id = ip.id
                WHERE ie.rule = 'relist_same_seller'
                  AND ip.inf_relist_same_seller > 0
                  AND ip.inf_likely_instant_sale = 0
                  AND ie.meta_json LIKE '%"revertsSaleRule": "likely_instant_sale"%'
                """
            ).fetchall()
        ]
        online_ids = [
            r[0]
            for r in con.execute(
                """
                SELECT DISTINCT ip.id
                FROM item_polls ip
                JOIN inference_events ie ON ie.item_poll_id = ip.id
                WHERE ie.rule = 'relist_same_seller'
                  AND ip.inf_relist_same_seller > 0
                  AND ip.inf_likely_non_instant_online = 0
                  AND ie.meta_json LIKE '%"revertsSaleRule": "likely_non_instant_online_sale"%'
                """
            ).fetchall()
        ]
        print(f"Instant relist undo polls to fix: {len(instant_ids)}")
        print(f"Non-instant online relist undo polls to fix: {len(online_ids)}")
        if args.dry_run:
            return
        if instant_ids:
            q = ",".join("?" * len(instant_ids))
            con.execute(f"UPDATE item_polls SET inf_likely_instant_sale = -1 WHERE id IN ({q})", instant_ids)
        if online_ids:
            q = ",".join("?" * len(online_ids))
            con.execute(
                f"UPDATE item_polls SET inf_likely_non_instant_online = -1 WHERE id IN ({q})",
                online_ids,
            )
        con.commit()
        print("Done.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
