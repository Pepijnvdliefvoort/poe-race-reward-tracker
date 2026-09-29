from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ML import model as model_mod
from ML import pipeline
from ML.estimator import EstimatorParams
from ML.market import load_market
from ML.synthetic import build_synthetic_db, open_readonly
from server import companion_track_record
from server.recommendation_service import RecommendationInputError, companion_track_record_summary, recommend_investments
from storage.db import Database


class RecommendInvestmentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.db = build_synthetic_db(cls.root, variants=20, days=150, poll_hours=12, seed=5)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _recommend(self, **overrides):
        request = {"wealth": 100, "currency": "mirror", "risk": "speculative", "mode": "ranked", "limit": 8}
        request.update(overrides)
        return recommend_investments(request, root_dir=self.root)

    def test_ranked_by_expected_return_per_day(self) -> None:
        out = self._recommend()
        recs = out["recommendations"]
        self.assertTrue(recs)
        self.assertEqual(out["ranking"]["method"], "estimator")
        per_day = [r["estimate"]["returnPerDayPct"] for r in recs]
        self.assertEqual(per_day, sorted(per_day, reverse=True))
        self.assertTrue(all(v > 0 for v in per_day))
        for r in recs:
            self.assertLessEqual(r["priceMirror"], 100 * 0.98)
            self.assertLessEqual(r["suggestedUnits"], r["maxUnits"])
            self.assertIn(r["category"], {"Quick flip", "Steady", "Slow hold", "Speculative"})

    def test_safe_profile_is_stricter(self) -> None:
        safe = self._recommend(risk="safe")
        spec = self._recommend()
        for r in safe["recommendations"]:
            self.assertGreaterEqual(r["estimate"]["sellProbability"], 0.5)
            self.assertIn(r["confidence"], {"medium", "strong"})
        # Same candidates minus the ones the stricter profile filters out.
        self.assertGreaterEqual(safe["skipped"]["risk_filtered"], spec["skipped"]["risk_filtered"])
        self.assertEqual(spec["skipped"]["risk_filtered"], 0)

    def test_portfolio_respects_unit_caps_and_budget(self) -> None:
        out = self._recommend(mode="portfolio", risk="balanced")
        plan = out["portfolio"]
        self.assertLessEqual(plan["deployedMirror"], 100)
        for pos in plan["positions"]:
            self.assertLessEqual(pos["portfolioUnits"], pos["maxUnits"])

    def test_invalid_input_is_rejected(self) -> None:
        with self.assertRaises(RecommendationInputError):
            self._recommend(wealth=0)
        with self.assertRaises(RecommendationInputError):
            self._recommend(risk="yolo")

    def test_enabled_model_drives_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_synthetic_db(root, variants=20, days=220, poll_hours=12, seed=3)
            con = open_readonly(db)
            try:
                market = load_market(con)
            finally:
                con.close()
            final_model, meta = pipeline.evaluate_and_train(market, EstimatorParams())
            model_mod.save(root, final_model, dict(meta, enabled=True, disabledReason=None))
            out = recommend_investments({"wealth": 100, "risk": "speculative"}, root_dir=root)
            self.assertEqual(out["ranking"]["method"], "model")
            recs = out["recommendations"]
            self.assertTrue(recs)
            self.assertTrue(all(r["rankingSource"] == "model" for r in recs))
            model_scores = [r["modelReturnPerDayPct"] for r in recs]
            self.assertEqual(model_scores, sorted(model_scores, reverse=True))

    def test_shown_picks_are_logged_once_per_week(self) -> None:
        con = Database(root_dir=self.root).connect()
        try:
            con.execute("DELETE FROM companion_picks")  # other tests in this class log picks too
            con.commit()
        finally:
            con.close()
        first = self._recommend(limit=3)
        self._recommend(limit=3)  # same picks again: no new rows
        con = Database(root_dir=self.root).connect()
        try:
            rows = con.execute("SELECT item_variant_id, best_rank, plan FROM companion_picks").fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), len(first["recommendations"]))
        self.assertEqual(sorted(int(r["best_rank"]) for r in rows), list(range(1, len(rows) + 1)))
        summary = companion_track_record_summary(root_dir=self.root)
        self.assertEqual(summary["logged"], len(rows))
        self.assertEqual(summary["pending"], len(rows))  # nothing has had time to sell yet
        self.assertEqual(len(summary["recent"]), len(rows))


class TrackRecordReplayTests(unittest.TestCase):
    def test_old_picks_are_replayed_on_later_history(self) -> None:
        from datetime import datetime, timezone

        from ML.estimator import estimate
        from ML.features import DAY, snapshots_at

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_synthetic_db(root, variants=12, days=150, poll_hours=12, seed=7)
            con = open_readonly(db)
            try:
                market = load_market(con)
            finally:
                con.close()
            params = EstimatorParams()
            ts = market.start_ts + 40 * DAY
            picks = sorted(((s, estimate(s, params)) for s in snapshots_at(market, ts)), key=lambda x: -x[1].return_per_day)[:4]
            con = Database(root_dir=root).connect()
            try:
                when = datetime.fromtimestamp(ts, tz=timezone.utc)
                companion_track_record.log_picks(con, picks, params, ranking_source="estimator", now=when)
                out = companion_track_record.evaluate_and_summarize(con, market, now=datetime.now(timezone.utc))
                stored = con.execute("SELECT COUNT(*) FROM companion_picks WHERE outcome_evaluated_at_utc IS NOT NULL").fetchone()[0]
            finally:
                con.close()
            # 40 + 60 days < 150 days of data: every pick has a final outcome.
            self.assertEqual(out["pending"], 0)
            self.assertEqual(out["evaluated"]["picks"], len(picks))
            self.assertEqual(stored, len(picks))
            self.assertTrue(all(p["status"] in {"sold", "unsold"} for p in out["recent"]))


if __name__ == "__main__":
    unittest.main()
