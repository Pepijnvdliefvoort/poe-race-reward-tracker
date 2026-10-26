from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from poller.poll_item_prices import (
    is_repeat_price_alert,
    seed_price_alert_cooldown_from_db,
    sellers_listed_at_price,
)
from storage.service import StorageService, VariantSpec


def _listing(seller: str, amount: float, currency: str = "divine") -> dict:
    return {"listing": {"account": {"name": seller}, "price": {"amount": amount, "currency": currency}}}


class PriceAlertDedupeTests(unittest.TestCase):
    def _repeat(self, last_alert, *, cycle: int, cheapest_raw, sellers, low_mirror=None, cooldown=6) -> bool:
        return is_repeat_price_alert(
            last_alert,
            cycle=cycle,
            low_mirror=cheapest_raw[2] if low_mirror is None else low_mirror,
            cheapest_raw=cheapest_raw,
            sellers_at_cheapest=sellers,
            cooldown_cycles=cooldown,
        )

    def test_first_alert_is_not_a_repeat(self) -> None:
        self.assertFalse(self._repeat(None, cycle=1, cheapest_raw=("divine", 100.0, 0.5), sellers={"A"}))

    def test_same_listing_same_price_stays_quiet_after_cooldown(self) -> None:
        last = (10, 0.5, "divine", 100.0, "A")
        for cycle in (11, 16, 17, 500):
            self.assertTrue(self._repeat(last, cycle=cycle, cheapest_raw=("divine", 100.0, 0.5), sellers={"A"}))

    def test_divine_ratio_drift_is_not_a_price_change(self) -> None:
        last = (10, 0.5, "divine", 100.0, "A")
        self.assertTrue(self._repeat(last, cycle=50, cheapest_raw=("divine", 100.0, 0.45), sellers={"A"}))

    def test_price_change_alerts_again(self) -> None:
        last = (10, 0.5, "divine", 100.0, "A")
        self.assertFalse(self._repeat(last, cycle=11, cheapest_raw=("divine", 90.0, 0.45), sellers={"A"}))

    def test_other_seller_same_price_waits_for_cooldown_only(self) -> None:
        last = (10, 0.5, "divine", 100.0, "A")
        self.assertTrue(self._repeat(last, cycle=12, cheapest_raw=("divine", 100.0, 0.5), sellers={"B"}))
        self.assertFalse(self._repeat(last, cycle=16, cheapest_raw=("divine", 100.0, 0.5), sellers={"B"}))

    def test_alerted_seller_still_tied_at_cheapest_stays_quiet(self) -> None:
        last = (10, 0.5, "divine", 100.0, "A")
        self.assertTrue(self._repeat(last, cycle=40, cheapest_raw=("divine", 100.0, 0.5), sellers={"A", "B"}))

    def test_legacy_row_without_seller_stays_quiet_at_same_price(self) -> None:
        last = (10, 0.5, "", 0.0, "")
        self.assertTrue(self._repeat(last, cycle=40, cheapest_raw=("divine", 100.0, 0.5), sellers={"A"}))
        self.assertFalse(self._repeat(last, cycle=40, cheapest_raw=("divine", 80.0, 0.4), sellers={"A"}))

    def test_sellers_listed_at_price(self) -> None:
        listings = [
            _listing("A", 100),
            _listing("B", 100),
            _listing("C", 101),
            _listing("D", 100, "mirror"),
            {"listing": {"price": {"amount": 100, "currency": "divine"}}},
        ]
        self.assertEqual(sellers_listed_at_price(listings, "divine", 100.0), {"A", "B"})
        self.assertEqual(sellers_listed_at_price(listings, "", 0.0), set())


class PriceAlertCooldownStorageTests(unittest.TestCase):
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
                )
            ]
        )
        self.variant_id = int(self.storage.list_variants()[0][0])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_listing_identity_survives_restart(self) -> None:
        self.storage.upsert_price_alert_cooldown(
            variant_id=self.variant_id,
            last_cycle=7,
            last_low_mirror=0.5,
            seller="A",
            currency="divine",
            amount=100.0,
        )
        state = seed_price_alert_cooldown_from_db(self.storage, {"hh": self.variant_id})
        self.assertEqual(state["hh"], (7, 0.5, "divine", 100.0, "A"))


if __name__ == "__main__":
    unittest.main()
