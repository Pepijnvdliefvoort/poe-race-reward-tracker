# ML-Assisted Ranking For Investment Companion

## Goal
Use a lightweight ML model to estimate the chance that an item is a profitable flip within a selected horizon (for example 7/14/30 days), then use that probability to rank suggestions in the investment companion.

This should improve recommendation quality versus pure heuristics while keeping the current logic as a safety fallback.

## Why This Feature
Current recommendations already use useful signals (price, trend, inferred sales, listing depth), but their weighting is manually tuned.
An ML layer can learn better combinations of these signals from historical outcomes and adapt as market behavior changes.

## Proposed Output
For each candidate item variant, produce:
- `profitProbNd`: Probability of profitable flip within N days
- `expectedProfitNd`: Optional expected mirror profit estimate for N days
- `mlConfidence`: Confidence/calibration signal to avoid over-trusting weak predictions

The companion can then rank by a hybrid score, for example:
- `finalScore = heuristicScore * 0.5 + normalizedMLScore * 0.5`

(Weights should be configurable and gradually shifted toward ML only after validation.)

## Implementation Steps
1. Define labels
- Label each historical poll snapshot with `1/0`: profitable flip achieved within N days.
- Start with one horizon (N=30) first, then expand to multiple horizons.

2. Build training dataset
- One row per item variant per poll time.
- Features available at that timestamp only (no future leakage):
  - current floor/median/high
  - listing ladder depth and spread
  - recent inferred sales counts (7d/30d)
  - trend and volatility
  - liquidity/supply proxies (`total_results`, `used_results`)
  - inference signal mix (confirmed transfer vs instant/non-instant)

3. Train simple baseline model
- Start with logistic regression or gradient-boosted trees.
- Use time-based train/validation/test split.
- Save model artifact + feature schema + version metadata.

4. Evaluate and calibrate
- Metrics: ROC-AUC, PR-AUC, precision@k, recall@k, calibration error.
- Business metrics: realized profit of top-k suggestions vs current heuristic baseline.

5. Integrate into companion API
- Load model at server startup.
- During recommendation generation, compute ML features and predictions.
- Blend ML score with current heuristic score.
- If model missing/fails, automatically fall back to heuristic-only.

6. Add monitoring and retraining loop
- Track online outcomes of suggested picks.
- Detect drift (feature drift, calibration drift, performance drop).
- Retrain periodically (for example weekly) and keep versioned rollback.

## Challenges And Possible Difficulties
- Label noise: inferred sales are probabilistic, not perfect ground truth.
- Market regime shifts: league start/merge and patch cycles can change behavior quickly.
- Data sparsity: rare items may have too little history for stable predictions.
- Leakage risk: accidentally using future data can inflate offline metrics.
- Class imbalance: truly profitable events may be less frequent than non-profitable ones.
- Operational safety: model-serving failures must not break recommendations.

## Risk Controls
- Keep heuristic fallback permanently available.
- Add strict feature timestamp checks in dataset builder.
- Use confidence thresholds (for low confidence, down-weight ML contribution).
- Start with shadow mode (log predictions without changing rankings), then A/B or phased rollout.

## Expected Result
Short term:
- More consistent ranking quality and fewer arbitrary ordering decisions.
- Better top-k suggestions than static manual weighting.

Medium term:
- Faster adaptation to changing markets.
- A measurable feedback loop: retraining from new outcomes improves suggestion quality over time.

## Suggested Milestones
- M1: Dataset builder + baseline label definition.
- M2: First offline model and benchmark report.
- M3: Shadow-mode integration in companion endpoint.
- M4: Hybrid ranking rollout with monitoring.
- M5: Periodic retraining automation.

## Definition Of Done
- Model improves at least one key business metric over heuristic baseline in backtests (for example top-5 realized profit or precision@5).
- API returns ML fields reliably and falls back safely when needed.
- Monitoring dashboard shows model version, calibration, and recent outcome quality.
