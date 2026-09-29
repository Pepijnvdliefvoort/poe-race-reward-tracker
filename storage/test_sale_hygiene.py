from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage.db import Database
from storage.sale_hygiene import apply_reverts, plan_reverts, revert_anomaly_day_sales

T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


class SaleHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.con = Database(root_dir=Path(self._tmp.name)).connect()
        self.con.execute("INSERT INTO items(name, created_at_utc) VALUES ('X', '2026-01-01T00:00:00+00:00')")
        self.con.execute("INSERT INTO item_variants(item_id, mode, display_name) VALUES (1, 'aa', 'X')")
        self._run = 0

    def tearDown(self) -> None:
        self.con.close()
        self._tmp.cleanup()

    def _poll(self, when: datetime, sellers: list[str]) -> int:
        self._run += 1
        ts = when.isoformat()
        self.con.execute(
            "INSERT INTO poll_runs(cycle_number, league, started_at_utc, divines_per_mirror) VALUES (?, 'S', ?, 1600)",
            (self._run, ts),
        )
        self.con.execute(
            "INSERT INTO item_polls(poll_run_id, item_variant_id, requested_at_utc, query_id) VALUES (?, 1, ?, 'q')",
            (self._run, ts),
        )
        poll_id = int(self.con.execute("SELECT last_insert_rowid()").fetchone()[0])
        for rank, seller in enumerate(sellers, start=1):
            self.con.execute(
                """INSERT INTO listing_snapshots(item_poll_id, rank, seller_name, price_text, amount, currency,
                       is_instant_buyout, fingerprint) VALUES (?, ?, ?, 'p', 5, 'mirror', 1, 'fp')""",
                (poll_id, rank, seller),
            )
        return poll_id

    def _sale(self, poll_id: int, when: datetime, seller: str, *, fp: str = "fp", rule: str = "likely_instant_sale",
              buyer: str | None = None) -> None:
        ts = when.isoformat()
        self.con.execute(
            """INSERT INTO sales(item_poll_id, item_variant_id, occurred_at_utc, recorded_at_utc, rule, fingerprint,
                   seller, buyer, mirror_equiv, price_amount, price_currency) VALUES (?, 1, ?, ?, ?, ?, ?, ?, 5, 5, 'mirror')""",
            (poll_id, ts, ts, rule, fp, seller, buyer or ""),
        )

    def _reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in plan_reverts(self.con):
            out[p.reason] = out.get(p.reason, 0) + 1
        return out

    def test_mass_vanish_burst_and_ping_pong(self) -> None:
        # Mass vanish: 5 of 6 listed sellers "sold" in one poll.
        p0 = self._poll(T0, [f"S{i}" for i in range(6)])
        p1 = self._poll(T0 + timedelta(hours=1), ["S5"])
        for i in range(5):
            self._sale(p1, T0 + timedelta(hours=1), f"S{i}", fp=f"fp{i}")
        # Seller burst: one seller, three sales in one poll; real sales from 2 sellers of 20 stay.
        p2 = self._poll(T0 + timedelta(days=1), [f"B{i}" for i in range(20)])
        p3 = self._poll(T0 + timedelta(days=1, hours=1), [f"B{i}" for i in range(2, 20)])
        for fp in ("a", "b", "c"):
            self._sale(p3, T0 + timedelta(days=1, hours=1), "B0", fp=fp)
        self._sale(p3, T0 + timedelta(days=1, hours=1), "B1", fp="d")
        # Ping-pong: A -> C and C -> A; a one-off D -> E stays.
        self._sale(p2, T0 + timedelta(days=1), "A", rule="confirmed_transfer", buyer="C")
        self._sale(p3, T0 + timedelta(days=1, hours=1), "C", rule="confirmed_transfer", buyer="A")
        self._sale(p3, T0 + timedelta(days=1, hours=1), "D", rule="confirmed_transfer", buyer="E")
        del p0

        self.assertEqual(self._reasons(), {"mass_vanish": 5, "seller_burst": 2, "transfer_ping_pong": 2})
        apply_reverts(self.con, plan_reverts(self.con))
        active = self.con.execute("SELECT COUNT(*) FROM sales WHERE reverted_at_utc IS NULL").fetchone()[0]
        self.assertEqual(active, 3)  # B0 once, B1, D -> E
        counters = self.con.execute(
            "SELECT inf_likely_instant_sale, inf_confirmed_transfer FROM item_polls WHERE id = ?", (p3,)
        ).fetchone()
        self.assertEqual(tuple(counters), (2, 1))
        self.assertEqual(plan_reverts(self.con), [])  # idempotent

    def test_anomaly_day_reverts_a_spike_spread_over_many_polls(self) -> None:
        for d in range(15):
            day = T0 + timedelta(days=d)
            for h in range(2):  # 2 sales a day, one seller per poll
                pid = self._poll(day + timedelta(hours=h), [f"S{d}{h}", "other"])
                self._sale(pid, day + timedelta(hours=h), f"S{d}{h}")
        spike = T0 + timedelta(days=15)
        for h in range(40):
            pid = self._poll(spike + timedelta(minutes=10 * h), [f"V{h}", "other"])
            self._sale(pid, spike + timedelta(minutes=10 * h), f"V{h}")

        self.assertEqual(self._reasons(), {"anomaly_day": 40})
        self.assertEqual(revert_anomaly_day_sales(self.con, T0.date().isoformat()), [])  # a normal day
        self.assertEqual(len(revert_anomaly_day_sales(self.con, spike.date().isoformat())), 40)
        active = self.con.execute("SELECT COUNT(*) FROM sales WHERE reverted_at_utc IS NULL").fetchone()[0]
        self.assertEqual(active, 30)


if __name__ == "__main__":
    unittest.main()
