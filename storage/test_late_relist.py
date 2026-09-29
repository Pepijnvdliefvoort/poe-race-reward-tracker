from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from poller.sale_inference_engine import evaluate_listing_transition
from storage.service import StorageService, VariantSpec


def _sig(seller: str = "A") -> dict:
    return {
        "fingerprint": "fp1",
        "seller": seller,
        "isInstant": True,
        "sellerOnline": True,
        "mirrorEquiv": 5.0,
        "priceAmount": 5.0,
        "priceCurrency": "mirror",
    }


def _row(seller: str = "A") -> dict:
    return {"fingerprint": "fp1", "sellerName": seller, "amount": 5.0, "currency": "mirror"}


class LateRelistReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = StorageService(root_dir=Path(self._tmp.name))
        self.storage.upsert_variants(
            [
                VariantSpec(
                    base_item_name="Headhunter",
                    mode="aa",
                    display_name="Headhunter",
                    sort_order=0,
                    icon_path=None,
                    image_name_filter=None,
                )
            ]
        )
        self.variant_id = int(self.storage.list_variants()[0][0])
        self.t0 = datetime.now(timezone.utc) - timedelta(hours=1)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, snapshots: list[list[dict]]) -> list[dict]:
        """Feed consecutive snapshots through the engine + storage; return per-cycle effective counts."""
        prev: list[dict] = []
        pending_instant: list[dict] = []
        pending_online: list[dict] = []
        out: list[dict] = []
        for cycle, curr in enumerate(snapshots, start=1):
            res, pending_instant, pending_online, _ = evaluate_listing_transition(
                item_key="k",
                cycle=cycle,
                prev_signals=prev,
                curr_signals=curr,
                pending_instant=pending_instant,
                pending_online=pending_online,
                baseline_mirror=5.0,
            )
            ts = (self.t0 + timedelta(minutes=5 * cycle)).isoformat()
            write = self.storage.write_poll_result(
                cycle_number=cycle,
                league="L",
                run_started_at_utc=ts,
                requested_at_utc=ts,
                divines_per_mirror=1650.0,
                top_ids_limit=20,
                inference_fetch_cap=100,
                variant_id=self.variant_id,
                query_id="q",
                total_results=len(curr),
                used_results=len(curr),
                unsupported_price_count=0,
                mirror_count=len(curr),
                lowest_mirror=5.0,
                median_mirror=5.0,
                highest_mirror=5.0,
                divine_count=0,
                lowest_divine=None,
                median_divine=None,
                highest_divine=None,
                inference_counts={"likelyInstantSale": res.likely_instant_sale},
                fetched_for_inference=len(curr),
                listing_preview_rows=[_row(str(s["seller"])) for s in curr],
                inference_events=res.events,
                inference_state=(curr, pending_instant, pending_online),
                late_relist_window_days=30,
            )
            out.append(write.inference_counts)
            prev = curr
        return out

    def _active_sales(self) -> int:
        con = self.storage._db.connect()
        try:
            row = con.execute("SELECT COUNT(*) FROM sales WHERE reverted_at_utc IS NULL").fetchone()
            return int(row[0])
        finally:
            con.close()

    def test_rule_2c_sale_not_reverted_while_other_copies_stay_listed(self) -> None:
        counts = self._run([[_sig(), _sig(), _sig()], [_sig(), _sig()], [_sig(), _sig()], [_sig(), _sig()]])
        self.assertEqual(counts[1].get("likelyInstantSale"), 1)
        self.assertEqual(counts[2].get("likelyInstantSale"), 0)
        self.assertEqual(counts[3].get("likelyInstantSale"), 0)
        self.assertEqual(self._active_sales(), 1)

    def test_pair_returning_after_absence_still_reverts_late(self) -> None:
        # Sold (vanished), gone for several polls, then the same seller relists the same roll.
        counts = self._run([[_sig()], [], [], [], [_sig()]])
        self.assertEqual(counts[1].get("likelyInstantSale"), 1)
        self.assertEqual(counts[4].get("likelyInstantSale"), -1)
        self.assertEqual(self._active_sales(), 0)

    def test_transfer_ping_pong_between_same_sellers_is_not_a_sale(self) -> None:
        # A -> B counts as a transfer; B -> A afterwards shows two traders relisting, so neither counts.
        counts = self._run([[_sig("A")], [_sig("B")], [_sig("A")]])
        self.assertEqual(counts[1].get("confirmedTransfer"), 1)
        self.assertEqual(counts[2].get("confirmedTransfer"), 0)
        self.assertEqual(self._active_sales(), 0)
        con = self.storage._db.connect()
        try:
            reasons = [r[0] for r in con.execute("SELECT reverted_reason FROM sales")]
            first_poll_transfers = con.execute(
                "SELECT inf_confirmed_transfer FROM item_polls ORDER BY id LIMIT 1 OFFSET 1"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(reasons, ["transfer_ping_pong"])
        self.assertEqual(first_poll_transfers, 0)

    def test_one_off_transfer_still_counts(self) -> None:
        counts = self._run([[_sig("A")], [_sig("B")], [_sig("B")]])
        self.assertEqual(counts[1].get("confirmedTransfer"), 1)
        self.assertEqual(self._active_sales(), 1)


if __name__ == "__main__":
    unittest.main()
