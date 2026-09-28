from __future__ import annotations

import contextlib
import io
import json
import math
import sqlite3
import tempfile
import unittest
from array import array
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ML import model as model_mod
from ML import pipeline
from ML.estimator import EstimatorParams, estimate, params_from_config
from ML.features import DAY, Snapshot, market_sale_rate, snapshot, snapshots_at
from ML.market import Market, PollPoint, SalePoint, VariantHistory, load_market
from ML.simulate import decision_times, realize_trade
from ML.synthetic import build_synthetic_db, open_readonly

T0 = 1_700_000_000.0


def _hist(polls: list[tuple[float, float | None]], sales: list[tuple[float, float]] | None = None, listings: int = 5) -> VariantHistory:
    h = VariantHistory(variant_id=1, base_item_name="X", display_name="X", mode="aa")
    h.polls = [
        PollPoint(
            ts=T0 + d * DAY, floor_mirror=p, total_results=listings, new_listing_rows=0,
            instant_ladder=array("f", [p] if p is not None else []),
        )
        for d, p in polls
    ]
    h.sales = [SalePoint(ts=T0 + d * DAY, price_mirror=p) for d, p in (sales or [])]
    h.finalize()
    return h


def _snap(**overrides) -> Snapshot:
    base = dict(
        variant_id=1, ts=T0, entry_price=10.0, entry_age_days=0.0, listing_anchor=10.0, sale_anchor=12.0,
        fair_value=12.0, sales_30d=3, sales_90d=6, recent_sale_prices=(12.0,) * 6, days_since_last_sale=2.0,
        total_listings=5, listings_change_30d=None, new_listings_per_day_30d=0.0, sale_momentum=None,
        floor_momentum=None, market_sale_rate_per_day=0.02,
    )
    base.update(overrides)
    return Snapshot(**base)


class FeatureTests(unittest.TestCase):
    def test_snapshot_ignores_data_after_t(self) -> None:
        past = [(d, 10.0) for d in range(0, 60)]
        future = [(d, 99.0) for d in range(61, 90)]
        sales_past = [(10, 11.0), (40, 12.0)]
        sales_future = [(65, 50.0), (70, 50.0)]
        t = T0 + 60 * DAY
        a = snapshot(_hist(past, sales_past), t, market_rate=0.01)
        b = snapshot(_hist(past + future, sales_past + sales_future), t, market_rate=0.01)
        self.assertEqual(a, b)

    def test_no_snapshot_without_recent_buyable_price(self) -> None:
        h = _hist([(0, 10.0), (1, None)])
        self.assertIsNone(snapshot(h, T0 + 20 * DAY, market_rate=0.01))
        self.assertIsNotNone(snapshot(h, T0 + 3 * DAY, market_rate=0.01))

    def test_fair_value_shrinks_toward_listings_when_sales_are_sparse(self) -> None:
        polls = [(d, 10.0) for d in range(0, 30)]
        one_sale = snapshot(_hist(polls, [(20, 20.0)]), T0 + 29 * DAY, market_rate=0.01)
        many_sales = snapshot(_hist(polls, [(d, 20.0) for d in range(10, 29)]), T0 + 29 * DAY, market_rate=0.01)
        self.assertLess(one_sale.fair_value, many_sales.fair_value)
        self.assertGreater(one_sale.fair_value, 10.0)


def _poll_db(root: Path, polls: list[list[tuple[str, str, float, int]]], sales: list[tuple[int, str, str, float]]) -> Path:
    """Tiny DB: one variant, one poll per day; `polls[i]` = [(seller, fingerprint, mirror price, instant)]."""
    from storage.db import Database

    db = Database(root_dir=root)
    con = db.connect()
    con.execute("INSERT INTO items(name, created_at_utc) VALUES ('X', '2026-01-01T00:00:00+00:00')")
    con.execute("INSERT INTO item_variants(item_id, mode, display_name) VALUES (1, 'aa', 'X')")
    poll_ids = []
    for day, listings in enumerate(polls):
        ts = f"2026-01-{day + 1:02d}T00:00:00+00:00"
        con.execute("INSERT INTO poll_runs(cycle_number, league, started_at_utc, divines_per_mirror) VALUES (?, 'S', ?, 1600)", (day + 1, ts))
        floor = min((p for _, _, p, _ in listings), default=None)
        con.execute(
            "INSERT INTO item_polls(poll_run_id, item_variant_id, requested_at_utc, query_id, lowest_mirror) VALUES (?, 1, ?, 'q', ?)",
            (day + 1, ts, floor),
        )
        poll_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        poll_ids.append(poll_id)
        for rank, (seller, fp, price, inst) in enumerate(sorted(listings, key=lambda x: x[2]), start=1):
            con.execute(
                """INSERT INTO listing_snapshots(item_poll_id, rank, seller_name, price_text, amount, currency,
                       is_instant_buyout, fingerprint) VALUES (?, ?, ?, 'p', ?, 'mirror', ?, ?)""",
                (poll_id, rank, seller, price, inst, fp),
            )
    for day, seller, fp, price in sales:
        ts = f"2026-01-{day + 1:02d}T00:00:00+00:00"
        con.execute(
            """INSERT INTO sales(item_poll_id, item_variant_id, occurred_at_utc, recorded_at_utc, rule, fingerprint,
                   seller, mirror_equiv) VALUES (?, 1, ?, ?, 'likely_instant_sale', ?, ?, ?)""",
            (poll_ids[day], ts, ts, fp, seller, price),
        )
    con.commit()
    con.close()
    return db.path


def _load(db: Path, **kw) -> Market:
    con = open_readonly(db)
    try:
        return load_market(con, **kw)
    finally:
        con.close()


class MarketLoaderTests(unittest.TestCase):
    def test_sale_dropped_only_when_same_roll_was_cheaper(self) -> None:
        # Day 0: seller A lists roll R at 1 mirror, seller B lists the same roll R at 0.4,
        #        seller C lists a different roll Q at 0.4. Day 1: A's and C's listings are gone.
        day0 = [("A", "R", 1.0, 1), ("B", "R", 0.4, 1), ("C", "Q", 0.4, 1), ("D", "S", 1.0, 1)]
        day1 = [("B", "R", 0.4, 1)]
        with tempfile.TemporaryDirectory() as tmp:
            db = _poll_db(Path(tmp), [day0, day1, day1], [(1, "A", "R", 1.0), (1, "D", "S", 1.0)])
            roll = _load(db)
            ratio = _load(db, sale_filter="floor_ratio")
        # A's 1-mirror sale is implausible (same roll R listed at 0.4 by B); D's roll S had no cheaper twin.
        self.assertEqual((roll.sales_dropped_implausible, roll.sales_kept), (1, 1))
        # The blunt floor-ratio filter drops both, because the cheapest listing of any roll was 0.4.
        self.assertEqual((ratio.sales_dropped_implausible, ratio.sales_kept), (2, 0))

    def test_listing_episodes_track_sold_removed_and_still_listed(self) -> None:
        polls = [
            [("A", "R", 2.0, 1), ("B", "Q", 3.0, 1), ("C", "S", 5.0, 1)],
            [("A", "R", 2.0, 1), ("B", "Q", 3.0, 1), ("C", "S", 5.0, 1)],
            [("C", "S", 5.0, 1)],  # A sold, B removed
            [("C", "S", 4.5, 1)],  # C repriced
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = _poll_db(Path(tmp), polls, [(2, "A", "R", 2.0)])
            hist = next(iter(_load(db).variants.values()))
        by_price = {e.price_mirror: e for e in hist.episodes}
        self.assertTrue(by_price[2.0].sold)
        self.assertFalse(by_price[3.0].sold)
        self.assertFalse(by_price[3.0].still_listed)
        self.assertFalse(by_price[5.0].sold)  # ended by a reprice, not a sale
        self.assertTrue(by_price[4.5].still_listed)
        self.assertEqual(list(hist.polls[0].instant_ladder), [2.0, 3.0, 5.0])
        self.assertAlmostEqual(hist.polls[0].instant_floor, 2.0)


class EstimatorTests(unittest.TestCase):
    def test_faster_selling_item_ranks_higher_at_same_margin(self) -> None:
        slow = estimate(_snap(sales_90d=1, recent_sale_prices=(12.0,)))
        fast = estimate(_snap(sales_90d=20, recent_sale_prices=(12.0,) * 20))
        self.assertAlmostEqual(slow.return_if_sold, fast.return_if_sold)
        self.assertGreater(fast.return_per_day, slow.return_per_day)
        self.assertLess(fast.expected_days, slow.expected_days)

    def test_quick_small_gain_beats_slow_large_gain(self) -> None:
        quick = estimate(_snap(fair_value=11.0, sales_90d=30, recent_sale_prices=(11.0,) * 30))
        slow = estimate(_snap(fair_value=20.0, sales_90d=1, recent_sale_prices=(20.0,), market_sale_rate_per_day=0.002))
        self.assertGreater(slow.return_if_sold, quick.return_if_sold)
        self.assertGreater(quick.return_per_day, slow.return_per_day)

    def test_no_margin_means_no_positive_return(self) -> None:
        est = estimate(_snap(fair_value=10.0, entry_price=10.0))
        self.assertLessEqual(est.return_per_day, 0.0)

    def test_sales_below_ask_do_not_count_toward_sell_rate(self) -> None:
        cheap_sales = estimate(_snap(recent_sale_prices=(9.0,) * 6))
        at_ask = estimate(_snap(recent_sale_prices=(12.0,) * 6))
        self.assertLess(cheap_sales.sale_rate_per_day, at_ask.sale_rate_per_day)

    def test_expected_days_never_exceeds_horizon(self) -> None:
        est = estimate(_snap(sales_90d=0, recent_sale_prices=(), market_sale_rate_per_day=1e-4))
        params = EstimatorParams()
        self.assertLessEqual(est.expected_days, params.horizon_days + params.listing_lag_days + 1e-9)

    def test_listings_ahead_of_us_slow_the_sale(self) -> None:
        alone = estimate(_snap(instant_ladder=(10.0,)))
        behind_wall = estimate(_snap(instant_ladder=(10.0, 10.5, 11.0, 11.0)))
        self.assertEqual(behind_wall.queue_ahead, 3)
        self.assertGreater(behind_wall.expected_days, alone.expected_days)

    def test_unsold_listings_at_similar_price_slow_the_sale(self) -> None:
        on = EstimatorParams(use_listing_evidence=True)
        base = estimate(_snap(), on)
        ask = base.ask_price
        stale = estimate(_snap(listing_evidence=((ask, 80.0, False), (ask * 1.1, 90.0, False))), on)
        selling = estimate(_snap(listing_evidence=((ask, 3.0, True), (ask * 1.05, 2.0, True))), on)
        self.assertLess(stale.sale_rate_per_day, base.sale_rate_per_day)
        self.assertGreater(selling.sale_rate_per_day, base.sale_rate_per_day)
        far_away = estimate(_snap(listing_evidence=((ask * 3, 90.0, False),)), on)
        self.assertAlmostEqual(far_away.sale_rate_per_day, base.sale_rate_per_day)
        # Off by default, but the counts are still reported (they feed the learned model).
        default = estimate(_snap(listing_evidence=((ask, 80.0, False),)))
        self.assertAlmostEqual(default.sale_rate_per_day, estimate(_snap()).sale_rate_per_day)
        self.assertAlmostEqual(default.similar_listing_days_90d, 80.0)

    def test_params_from_config_clamps(self) -> None:
        p = params_from_config({"invest_horizon_days": 5000, "invest_fee_pct": -3, "invest_undercut_pct": "x"})
        self.assertEqual(p.horizon_days, 180.0)
        self.assertEqual(p.fee_pct, 0.0)
        self.assertEqual(p.undercut_pct, EstimatorParams().undercut_pct)


class SimulationTests(unittest.TestCase):
    def test_sells_at_first_later_sale_at_or_above_ask(self) -> None:
        h = _hist([(d, 10.0) for d in range(0, 80)], [(3, 9.0), (5, 12.5), (8, 20.0)])
        snap = _snap(ts=T0 + 1 * DAY)
        out = realize_trade(h, snap, ask=12.0, horizon_days=60, fee_pct=0.0)
        self.assertTrue(out.sold)
        self.assertAlmostEqual(out.days, 4.0)
        self.assertAlmostEqual(out.ret, 0.2)

    def test_sales_at_or_before_entry_are_ignored(self) -> None:
        h = _hist([(d, 10.0) for d in range(0, 80)], [(1, 50.0)])
        out = realize_trade(h, _snap(ts=T0 + 1 * DAY), ask=12.0, horizon_days=60, fee_pct=0.0)
        self.assertFalse(out.sold)

    def test_unsold_is_marked_to_market_at_horizon(self) -> None:
        polls = [(d, 10.0) for d in range(0, 40)] + [(d, 8.0) for d in range(40, 80)]
        out = realize_trade(_hist(polls, [(5, 11.0)]), _snap(ts=T0), ask=12.0, horizon_days=60, fee_pct=0.0)
        self.assertFalse(out.sold)
        self.assertEqual(out.days, 60)
        self.assertAlmostEqual(out.ret, -0.2)

    def test_unsold_never_marked_above_entry(self) -> None:
        polls = [(d, 10.0) for d in range(0, 40)] + [(d, 30.0) for d in range(40, 80)]
        out = realize_trade(_hist(polls), _snap(ts=T0), ask=12.0, horizon_days=60, fee_pct=0.0)
        self.assertFalse(out.sold)
        self.assertAlmostEqual(out.ret, 0.0)

    def test_decision_times_leave_room_for_full_horizon(self) -> None:
        market = Market(variants={}, start_ts=T0, end_ts=T0 + 200 * DAY)
        times = decision_times(market, horizon_days=60)
        self.assertTrue(times)
        self.assertLessEqual(max(times) + 60 * DAY, market.end_ts)
        self.assertGreaterEqual(min(times), T0 + 30 * DAY)


class WalkForwardTests(unittest.TestCase):
    def test_training_rows_are_known_before_each_decision(self) -> None:
        params = EstimatorParams(horizon_days=30)
        rows = [
            model_mod.LabeledRow(ts=T0 + d * DAY, variant_id=v, features=[0.0] * len(model_mod.MODEL_FEATURES), label=0.01)
            for d in range(0, 200, 7)
            for v in range(10)
        ]
        seen: list[float] = []

        def fake_fit(train):
            seen.append(max(r.ts for r in train))
            return object()

        scorer = model_mod.WalkForwardScorer(rows, params)
        decision = T0 + 150 * DAY
        with mock.patch.object(model_mod, "fit", fake_fit), mock.patch.object(model_mod, "predict", lambda m, s, p: {}):
            scorer([_snap(ts=decision)])
        self.assertTrue(seen)
        self.assertLessEqual(seen[0], decision - params.horizon_days * DAY)
        self.assertIn(decision, scorer.active_times)


class PipelineTests(unittest.TestCase):
    def test_short_history_keeps_model_disabled_and_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_synthetic_db(root, variants=8, days=120, poll_hours=12)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = pipeline.main(["--db", str(db), "--root", str(root)])
            self.assertEqual(rc, 0)
            meta = json.loads((model_mod.model_dir(root) / model_mod.META_FILE).read_text(encoding="utf-8"))
            self.assertFalse(meta["enabled"])
            self.assertTrue(meta["disabledReason"])
            model, _meta, reason = model_mod.load_for_serving(root)
            self.assertIsNone(model)
            self.assertTrue(reason)

    def test_estimator_beats_random_on_synthetic_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_synthetic_db(root, variants=20, days=220, poll_hours=12, seed=3)
            meta = pipeline.run(db, root)
            weeks = meta["backtest"]["allWeeks"]
            self.assertGreater(weeks["estimator"]["returnPerDay"], weeks["random"]["returnPerDay"])
            self.assertGreater(meta["data"]["modelActiveWeeks"], 0)

    def test_enabled_model_is_served_only_with_matching_sklearn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_synthetic_db(root, variants=20, days=220, poll_hours=12, seed=3)
            con = open_readonly(db)
            try:
                market = load_market(con)
            finally:
                con.close()
            params = EstimatorParams()
            final_model, meta = pipeline.evaluate_and_train(market, params)
            self.assertIsNotNone(final_model)
            model_mod.save(root, final_model, dict(meta, enabled=True, disabledReason=None))
            model, _m, reason = model_mod.load_for_serving(root)
            self.assertIsNotNone(model, reason)

            snaps = snapshots_at(market, market.end_ts)
            preds = model_mod.predict(model, snaps, params)
            self.assertEqual(set(preds), {s.variant_id for s in snaps})
            self.assertTrue(all(math.isfinite(v) for v in preds.values()))

            model_mod.save_meta_only(root, dict(meta, enabled=True, sklearnVersion="0.0.0"))
            model, _m, reason = model_mod.load_for_serving(root)
            self.assertIsNone(model)
            self.assertIn("sklearn", reason)


if __name__ == "__main__":
    unittest.main()
