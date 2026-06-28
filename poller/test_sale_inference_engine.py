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


if __name__ == "__main__":
    unittest.main()
