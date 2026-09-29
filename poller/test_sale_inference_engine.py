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

    def test_count_decrease_delta_2_credits_one_sale_per_seller(self) -> None:
        """Seller had 3 listings; 2 gone in one poll, 1 remains -> 1 sale (rule 9), 2 without the cap."""
        prev_signals = [self._make_signal("nacho", 7.0, signal_count=3)]
        curr_signals = [self._make_signal("nacho", 8.0)]
        kwargs = dict(
            item_key="StormCloud",
            cycle=100,
            prev_signals=prev_signals,
            curr_signals=curr_signals,
            pending_instant=[],
            baseline_mirror=9.0,
            snapshot_truncated=False,
        )
        result, new_pend, _, _ = evaluate_listing_transition(**kwargs)
        self.assertEqual(result.likely_instant_sale, 1)
        self.assertEqual(_rules(result).count("likely_instant_sale"), 1)
        self.assertEqual(_rules(result).count("seller_burst_ignored"), 1)
        self.assertEqual(len(new_pend), 0)

        uncapped, _, _, _ = evaluate_listing_transition(**kwargs, max_sales_per_seller=0)
        self.assertEqual(uncapped.likely_instant_sale, 2)

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


class SaleInferenceEngineNonInstantOnlineGraceTests(unittest.TestCase):
    """Rule 4b: defer crediting a non-instant online vanish so a quick relist isn't a false sale."""

    def _prev_signal(self, seller: str = "seller1") -> dict:
        return {
            "fingerprint": "fp1",
            "seller": seller,
            "isInstant": False,
            "mirrorEquiv": 5.0,
            "priceAmount": 5.0,
            "priceCurrency": "mirror",
        }

    def test_vanish_defers_credit_when_grace_enabled(self) -> None:
        """A non-instant online vanish holds as pending instead of crediting immediately."""
        result, _, new_pending_online, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=1,
            prev_signals=[self._prev_signal()],
            curr_signals=[],
            pending_instant=[],
            pending_online=[],
            seller_online_probe={"seller1": True},
            baseline_mirror=5.0,
            non_instant_online_grace_polls=1,
        )
        self.assertEqual(result.likely_non_instant_online, 0)
        self.assertFalse(any(ev.get("rule") == "likely_non_instant_online_sale" for ev in result.events))
        self.assertTrue(any(ev.get("rule") == "non_instant_online_removed_pending" for ev in result.events))
        self.assertEqual(len(new_pending_online), 1)
        self.assertFalse(new_pending_online[0]["countedImmediate"])

    def test_relist_within_grace_is_fetch_jitter_not_revert(self) -> None:
        """A same-seller reappearance within the grace window is jitter, not a sale + revert pair."""
        pending = [
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "removed_cycle": 1,
                "countedImmediate": False,
                "jitterGracePolls": 1,
                "mirrorEquiv": 5.0,
                "priceAmount": 5.0,
                "priceCurrency": "mirror",
            }
        ]
        result, _, new_pending_online, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[],
            curr_signals=[self._prev_signal()],
            pending_instant=[],
            pending_online=pending,
            non_instant_online_grace_polls=1,
        )
        self.assertEqual(result.likely_non_instant_online, 0)
        self.assertEqual(result.relist_same_seller, 0)
        self.assertTrue(any(ev.get("rule") == "fetch_jitter_relist" for ev in result.events))
        self.assertEqual(len(new_pending_online), 0)

    def test_credit_after_grace_elapses_without_reappearance(self) -> None:
        """No reappearance within the grace window credits the sale on the next cycle."""
        pending = [
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "removed_cycle": 1,
                "countedImmediate": False,
                "jitterGracePolls": 1,
                "mirrorEquiv": 5.0,
                "priceAmount": 5.0,
                "priceCurrency": "mirror",
            }
        ]
        result, _, new_pending_online, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=3,
            prev_signals=[],
            curr_signals=[],
            pending_instant=[],
            pending_online=pending,
            non_instant_online_grace_polls=1,
        )
        self.assertEqual(result.likely_non_instant_online, 1)
        self.assertTrue(any(ev.get("rule") == "likely_non_instant_online_sale" for ev in result.events))
        self.assertEqual(len(new_pending_online), 0)

    def test_late_relist_after_credit_still_reverts(self) -> None:
        """Once counted immediate (grace elapsed), a later relist still reverts the sale."""
        pending = [
            {
                "fingerprint": "fp1",
                "seller": "seller1",
                "removed_cycle": 1,
                "countedImmediate": True,
                "jitterGracePolls": 0,
                "mirrorEquiv": 5.0,
                "priceAmount": 5.0,
                "priceCurrency": "mirror",
            }
        ]
        result, _, new_pending_online, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=4,
            prev_signals=[],
            curr_signals=[self._prev_signal()],
            pending_instant=[],
            pending_online=pending,
            non_instant_online_grace_polls=1,
        )
        self.assertEqual(result.likely_non_instant_online, -1)
        self.assertEqual(result.relist_same_seller, 1)
        self.assertTrue(any(ev.get("rule") == "relist_same_seller" for ev in result.events))
        self.assertEqual(len(new_pending_online), 0)


if __name__ == "__main__":
    unittest.main()


def _sig(fp: str, seller: str, price: float, *, instant: bool = True, online: bool = False) -> dict:
    return {
        "fingerprint": fp,
        "seller": seller,
        "isInstant": instant,
        "sellerOnline": online,
        "mirrorEquiv": price,
        "priceAmount": price,
        "priceCurrency": "mirror",
    }


def _rules(result) -> list[str]:
    return [str(ev.get("rule") or "") for ev in result.events]


class SaleInferenceEngineSellerSwapTests(unittest.TestCase):
    """Rule 1: the same roll moves from one sole seller to another."""

    def test_sole_seller_swap_counts_confirmed_transfer(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0)],
            curr_signals=[_sig("fp1", "B", 6.0)],
            pending_instant=[],
        )
        self.assertEqual(result.confirmed_transfer, 1)
        # The seller-A disappearance is part of the transfer, not a separate instant sale.
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertEqual(pending_instant, [])
        ev = next(e for e in result.events if e["rule"] == "confirmed_transfer")
        self.assertEqual((ev["from_seller"], ev["to_seller"]), ("A", "B"))

    def test_no_transfer_when_roll_had_multiple_sellers(self) -> None:
        result, _, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0), _sig("fp1", "C", 5.5)],
            curr_signals=[_sig("fp1", "B", 6.0)],
            pending_instant=[],
        )
        self.assertEqual(result.confirmed_transfer, 0)


class SaleInferenceEngineTruncatedSnapshotTests(unittest.TestCase):
    """Rules 2a/2b and the truncation cutoff guard."""

    def test_vanish_near_truncation_cutoff_is_ignored(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0), _sig("fp2", "B", 9.8)],
            curr_signals=[_sig("fp1", "A", 5.0)],
            pending_instant=[],
            snapshot_truncated=True,
            truncation_cutoff_mirror=10.0,
        )
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertEqual(pending_instant, [])
        self.assertEqual(result.events, [])

    def test_mid_ladder_instant_vanish_is_ignored_when_truncated(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0), _sig("fp2", "B", 8.0)],
            curr_signals=[_sig("fp1", "A", 5.0)],
            pending_instant=[],
            snapshot_truncated=True,
            truncation_cutoff_mirror=20.0,
        )
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertEqual(pending_instant, [])

    def test_near_floor_vanish_defers_credit_when_truncated(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0), _sig("fp2", "B", 5.5)],
            curr_signals=[_sig("fp2", "B", 5.5)],
            pending_instant=[],
            snapshot_truncated=True,
            truncation_cutoff_mirror=20.0,
            fetch_jitter_grace_polls=2,
        )
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertIn("instant_listing_removed_pending", _rules(result))
        self.assertEqual(len(pending_instant), 1)
        self.assertFalse(pending_instant[0]["countedImmediate"])
        self.assertEqual(pending_instant[0]["jitterGracePolls"], 2)

    def _deferred_pending(self) -> list[dict]:
        return [
            {
                "fingerprint": "fp1",
                "seller": "A",
                "removed_cycle": 1,
                "countedImmediate": False,
                "jitterGracePolls": 2,
                "mirrorEquiv": 5.0,
                "priceAmount": 5.0,
                "priceCurrency": "mirror",
            }
        ]

    def test_deferred_instant_stays_pending_within_grace(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=3,
            prev_signals=[],
            curr_signals=[],
            pending_instant=self._deferred_pending(),
        )
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertEqual(len(pending_instant), 1)

    def test_deferred_instant_credits_after_grace(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=4,
            prev_signals=[],
            curr_signals=[],
            pending_instant=self._deferred_pending(),
        )
        self.assertEqual(result.likely_instant_sale, 1)
        self.assertEqual(pending_instant, [])

    def test_deferred_instant_reappearing_is_fetch_jitter(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[],
            curr_signals=[_sig("fp1", "A", 5.0)],
            pending_instant=self._deferred_pending(),
        )
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertEqual(result.relist_same_seller, 0)
        self.assertIn("fetch_jitter_relist", _rules(result))
        self.assertEqual(pending_instant, [])

    def test_count_decrease_rule_skipped_when_truncated(self) -> None:
        result, _, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0)] * 3,
            curr_signals=[_sig("fp1", "A", 5.0)] * 2,
            pending_instant=[],
            snapshot_truncated=True,
            truncation_cutoff_mirror=100.0,
        )
        self.assertEqual(result.likely_instant_sale, 0)


class SaleInferenceEngineGuardTests(unittest.TestCase):
    def test_instant_vanish_far_above_baseline_is_unlist(self) -> None:
        result, pending_instant, _, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 20.0), _sig("fp2", "B", 12.0)],
            curr_signals=[_sig("fp2", "B", 12.0)],
            pending_instant=[],
            baseline_mirror=12.0,
        )
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertEqual(pending_instant, [])
        self.assertIn("unlisted_above_baseline", _rules(result))

    def test_offline_non_instant_vanish_is_inconclusive(self) -> None:
        result, _, pending_online, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0, instant=False, online=False)],
            curr_signals=[],
            pending_instant=[],
        )
        self.assertEqual(result.non_instant_removed, 1)
        self.assertEqual(result.likely_non_instant_online, 0)
        self.assertEqual(pending_online, [])
        self.assertIn("non_instant_removed_inconclusive", _rules(result))

    def test_online_probe_overrides_stale_snapshot_flag(self) -> None:
        result, _, pending_online, _ = evaluate_listing_transition(
            item_key="Item",
            cycle=2,
            prev_signals=[_sig("fp1", "A", 5.0, instant=False, online=True)],
            curr_signals=[],
            pending_instant=[],
            seller_online_probe={"A": False},
        )
        self.assertEqual(result.non_instant_removed, 1)
        self.assertEqual(pending_online, [])


class SaleInferenceEngineSanityCapTests(unittest.TestCase):
    """Rules 8 (mass vanish) and 9 (seller burst)."""

    def _transition(self, prev: list[dict], curr: list[dict], **kw):
        return evaluate_listing_transition(
            item_key="X",
            cycle=50,
            prev_signals=prev,
            curr_signals=curr,
            pending_instant=[],
            pending_online=[],
            baseline_mirror=10.0,
            snapshot_truncated=False,
            **kw,
        )

    def test_most_sellers_vanishing_at_once_is_not_a_sale(self) -> None:
        # 5 of 6 sellers gone in one poll: a trade-site glitch, not five buyers.
        prev = [_sig(f"fp{i}", f"S{i}", 10.0 + i * 0.1) for i in range(6)]
        curr = [prev[5]]
        result, new_pend, _, _ = self._transition(prev, curr)
        self.assertEqual(result.likely_instant_sale, 0)
        self.assertEqual(_rules(result).count("mass_vanish_ignored"), 5)
        self.assertEqual(new_pend, [])  # nothing left for a later relist to revert

    def test_several_sales_in_a_big_market_still_count(self) -> None:
        # 4 of 20 sellers sold (20%): real sales after e.g. poller downtime.
        prev = [_sig(f"fp{i}", f"S{i}", 10.0 + i * 0.01) for i in range(20)]
        curr = prev[4:]
        result, _, _, _ = self._transition(prev, curr)
        self.assertEqual(result.likely_instant_sale, 4)
        self.assertNotIn("mass_vanish_ignored", _rules(result))

    def test_three_sellers_in_a_tiny_market_still_count(self) -> None:
        prev = [_sig(f"fp{i}", f"S{i}", 10.0) for i in range(3)]
        result, _, _, _ = self._transition(prev, [])
        self.assertEqual(result.likely_instant_sale, 3)

    def test_one_seller_pulling_several_copies_counts_once(self) -> None:
        prev = [_sig("fpA", "Glazer", 10.0), _sig("fpB", "Glazer", 10.0), _sig("fpC", "Glazer", 10.0)]
        prev += [_sig(f"fp{i}", f"S{i}", 11.0) for i in range(10)]
        curr = prev[3:]
        result, new_pend, _, _ = self._transition(prev, curr)
        self.assertEqual(result.likely_instant_sale, 1)
        self.assertEqual(_rules(result).count("seller_burst_ignored"), 2)
        # Only the credited removal keeps a pending (so a relist can still undo it).
        self.assertEqual(len(new_pend), 1)
        credited = next(ev for ev in result.events if ev.get("rule") == "likely_instant_sale")
        self.assertEqual(new_pend[0]["fingerprint"], credited["fingerprint"])
