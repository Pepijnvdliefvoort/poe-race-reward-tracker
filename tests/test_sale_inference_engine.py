"""Unit tests for sale_inference_engine listing transition rules."""

from __future__ import annotations

import unittest

from poller.sale_inference_engine import (
    _count_multi_seller_fingerprints,
    evaluate_listing_transition,
)


class SaleInferenceRulesTest(unittest.TestCase):
    def test_rule1_confirmed_transfer(self) -> None:
        prev = [{"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 1.0}]
        curr = [{"fingerprint": "fp1", "seller": "bob", "isInstant": True, "mirrorEquiv": 1.0}]
        r, pending, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=2,
            prev_signals=prev,
            curr_signals=curr,
            pending_instant=[],
        )
        self.assertEqual(r.confirmed_transfer, 1)
        self.assertEqual(r.likely_instant_sale, 0)
        self.assertEqual(len(pending), 0)

    def test_rule2_instant_vanished_then_absent(self) -> None:
        prev = [{"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 1.0}]
        curr: list[dict] = []
        r1, pend1, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=1,
            prev_signals=prev,
            curr_signals=curr,
            pending_instant=[],
        )
        self.assertEqual(r1.likely_instant_sale, 1)
        self.assertEqual(len(pend1), 1)
        r2, pend2, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=2,
            prev_signals=curr,
            curr_signals=curr,
            pending_instant=pend1,
        )
        self.assertEqual(r2.likely_instant_sale, 0)
        self.assertEqual(len(pend2), 0)

    def test_rule3_relist_same_seller(self) -> None:
        prev = [{"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 1.0}]
        curr_empty: list[dict] = []
        r1, pend1, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=1,
            prev_signals=prev,
            curr_signals=curr_empty,
            pending_instant=[],
        )
        self.assertEqual(len(pend1), 1)
        self.assertEqual(r1.likely_instant_sale, 1)
        curr_back = [{"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 1.1}]
        r2, pend2, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=2,
            prev_signals=curr_empty,
            curr_signals=curr_back,
            pending_instant=pend1,
        )
        self.assertEqual(r2.relist_same_seller, 1)
        self.assertEqual(r2.likely_instant_sale, -1)
        self.assertEqual(len(pend2), 0)

    def test_rule4_non_instant_removed(self) -> None:
        prev = [{"fingerprint": "fp1", "seller": "alice", "isInstant": False, "mirrorEquiv": 1.0}]
        curr: list[dict] = []
        r, pending, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=2,
            prev_signals=prev,
            curr_signals=curr,
            pending_instant=[],
        )
        self.assertEqual(r.non_instant_removed, 1)
        self.assertEqual(len(pending), 0)

    def test_reprice_same_seller(self) -> None:
        prev = [{"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 10.0}]
        curr = [{"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 11.0}]
        r, pending, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=2,
            prev_signals=prev,
            curr_signals=curr,
            pending_instant=[],
        )
        self.assertGreaterEqual(r.reprice_same_seller, 1)
        self.assertEqual(len(pending), 0)

    def test_multi_seller_same_fingerprint(self) -> None:
        curr = [
            {"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 1.0},
            {"fingerprint": "fp1", "seller": "bob", "isInstant": True, "mirrorEquiv": 1.1},
        ]
        self.assertEqual(_count_multi_seller_fingerprints(curr), 1)

    def test_new_listing_rows(self) -> None:
        prev = [{"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 1.0}]
        curr = [
            {"fingerprint": "fp1", "seller": "alice", "isInstant": True, "mirrorEquiv": 1.0},
            {"fingerprint": "fp2", "seller": "carol", "isInstant": True, "mirrorEquiv": 2.0},
        ]
        r, _, _ = evaluate_listing_transition(
            item_key="Item::aa",
            cycle=2,
            prev_signals=prev,
            curr_signals=curr,
            pending_instant=[],
        )
        self.assertEqual(r.new_listing_rows, 1)


if __name__ == "__main__":
    unittest.main()
