from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ML import model as model_mod
from ML import pipeline
from ML.estimator import EstimatorParams
from ML.market import load_market
from ML.synthetic import build_synthetic_db, open_readonly
from server.recommendation_service import RecommendationInputError, recommend_investments


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


if __name__ == "__main__":
    unittest.main()
