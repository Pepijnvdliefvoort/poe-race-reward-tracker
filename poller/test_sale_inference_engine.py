from __future__ import annotations

import unittest

from poller.sale_inference_engine import evaluate_listing_transition


class SaleInferenceEngineRepriceTests(unittest.TestCase):
    def test_reprice_skipped_when_pair_has_multiple_listings(self) -> None:
        prev_signals = [
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "isInstant": False,
                "mirrorEquiv": 4.0,
                "priceAmount": 4.0,
                "priceCurrency": "mirror",
                "signalCount": 2,
            }
        ]
        curr_signals = [
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "isInstant": False,
                "mirrorEquiv": 3.9,
                "priceAmount": 3.9,
                "priceCurrency": "mirror",
            },
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "isInstant": False,
                "mirrorEquiv": 4.0,
                "priceAmount": 4.0,
                "priceCurrency": "mirror",
            },
        ]

        result, _, _, _ = evaluate_listing_transition(
            item_key="Brightbeak",
            cycle=101,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            pending_online=[],
        )

        self.assertEqual(result.reprice_same_seller, 0)
        self.assertFalse(any(str(ev.get("rule") or "") == "reprice_same_seller" for ev in result.events))

    def test_reprice_detected_for_unambiguous_single_listing(self) -> None:
        prev_signals = [
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "isInstant": False,
                "mirrorEquiv": 4.0,
                "priceAmount": 4.0,
                "priceCurrency": "mirror",
            }
        ]
        curr_signals = [
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "isInstant": False,
                "mirrorEquiv": 3.9,
                "priceAmount": 3.9,
                "priceCurrency": "mirror",
            }
        ]

        result, _, _, _ = evaluate_listing_transition(
            item_key="Brightbeak",
            cycle=102,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            pending_online=[],
        )

        self.assertEqual(result.reprice_same_seller, 1)
        self.assertTrue(any(str(ev.get("rule") or "") == "reprice_same_seller" for ev in result.events))


class SaleInferenceEngineMultiListingCountDecreaseTests(unittest.TestCase):
    """Rule 2c: seller holds multiple copies and sells one while keeping others."""

    def _make_signal(self, seller: str, price: float, *, signal_count: int = 1) -> dict:
        return {
            "fingerprint": "fp1",
            "seller": seller,
            "isInstant": True,
            "mirrorEquiv": price,
            "priceAmount": price,
            "priceCurrency": "mirror",
            "signalCount": signal_count,
        }

    def test_count_decrease_credits_instant_sale(self) -> None:
        """Seller had 2 listings (7m, 8m); 7m sold, 8m remains -> 1 sale."""
        prev_signals = [self._make_signal("nacho", 7.0, signal_count=2)]
        curr_signals = [
            self._make_signal("nacho", 8.0),
            self._make_signal("other", 9.0),
        ]
        result, new_pend, _, _ = evaluate_listing_transition(
            item_key="StormCloud",
            cycle=100,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            pending_online=[],
            snapshot_truncated=False,
            baseline_mirror=9.0,
        )
        self.assertEqual(result.likely_instant_sale, 1)
        sale_events = [ev for ev in result.events if ev.get("rule") == "likely_instant_sale"]
        self.assertEqual(len(sale_events), 1)
        self.assertEqual(sale_events[0]["mirrorEquiv"], 7.0)
        # No pending added because seller is still present; a pending would be immediately
        # reverted as relist_same_seller on the next cycle.
        self.assertEqual(len(new_pend), 0)

    def test_count_decrease_delta_2_credits_two_sales(self) -> None:
        """Seller had 3 listings; 2 sold, 1 remains -> 2 sales."""
        prev_signals = [self._make_signal("nacho", 7.0, signal_count=3)]
        curr_signals = [self._make_signal("nacho", 8.0)]
        result, new_pend, _, _ = evaluate_listing_transition(
            item_key="StormCloud",
            cycle=100,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            baseline_mirror=9.0,
            snapshot_truncated=False,
        )
        self.assertEqual(result.likely_instant_sale, 2)
        self.assertEqual(len([ev for ev in result.events if ev.get("rule") == "likely_instant_sale"]), 2)
        self.assertEqual(len(new_pend), 0)

    def test_count_increase_does_not_credit_sale(self) -> None:
        """Seller added a new listing (count went up) -> no sale."""
        prev_signals = [self._make_signal("nacho", 8.0, signal_count=1)]
        curr_signals = [self._make_signal("nacho", 7.0), self._make_signal("nacho", 8.0)]
        result, _, _, _ = evaluate_listing_transition(
            item_key="StormCloud",
            cycle=100,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            baseline_mirror=9.0,
            snapshot_truncated=False,
        )
        self.assertEqual(result.likely_instant_sale, 0)

    def test_skipped_when_snapshot_truncated(self) -> None:
        """Rule 2c must not fire on truncated snapshots to avoid fetch-window false positives."""
        prev_signals = [self._make_signal("nacho", 7.0, signal_count=2)]
        curr_signals = [self._make_signal("nacho", 8.0)]
        result, _, _, _ = evaluate_listing_transition(
            item_key="StormCloud",
            cycle=100,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            baseline_mirror=9.0,
            snapshot_truncated=True,
            truncation_cutoff_mirror=20.0,
        )
        self.assertEqual(result.likely_instant_sale, 0)

    def test_not_counted_when_price_above_baseline(self) -> None:
        """Price well above baseline is blocked by the unlisted_above_baseline guard."""
        prev_signals = [self._make_signal("nacho", 20.0, signal_count=2)]
        curr_signals = [self._make_signal("nacho", 21.0)]
        result, _, _, _ = evaluate_listing_transition(
            item_key="StormCloud",
            cycle=100,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            baseline_mirror=9.0,
            sale_max_above_baseline_pct=30.0,
            snapshot_truncated=False,
        )
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertTrue(any(ev.get("rule") == "unlisted_above_baseline" for ev in result.events))


if __name__ == "__main__":
    unittest.main()
