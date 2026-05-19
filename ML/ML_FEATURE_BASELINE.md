# ML Feature Baseline (Thin-Market, Sale-Anchored)

Last updated: 2026-05-19
Owner: poe-market-flips
Status: Baseline specification for future ML implementation

## Implementation Progress Tracker

### Done
- [x] Baseline specification created in this file.
- [x] First-step offline dataset builder implemented: ML/build_training_dataset.py.
- [x] Leakage-safe ordering applied in builder (features computed from <= t, labels from (t, t+30d]).
- [x] First output contract defined and implemented:
  - training CSV: ML/training_30d.csv
  - metadata JSON: ML/training_30d.meta.json
- [x] Builder smoke test executed successfully (2026-05-19):
  - generated rows: 90138
  - outputs written to ML/training_30d.csv and ML/training_30d.meta.json
- [x] Dataset profiling checks implemented and validated (2026-05-19):
  - usedGreaterThanTotal=0 after bounded feature handling
  - usedExceedsTotalFlagCount=392 retained for diagnostics
  - acceptance ratio bounds checks clean
- [x] Anomaly drilldown export added: ML/extract_profile_anomalies.py.
- [x] Baseline training pipeline implemented: ML/train_model.py.
- [x] First baseline training run completed (2026-05-19):
  - classifier model: ML/model_sellprob_30d.pkl
  - regressor model: ML/model_execprice_30d.pkl
  - report: ML/training_30d.training_report.json
  - classifier test: PR-AUC=0.1932, ROC-AUC=0.7884, Brier=0.2168
  - regressor test: MAE=8.6530, MAPE=0.7827
- [x] Offline backtest script and benchmark artifact implemented: ML/backtest_recommendations.py.
- [x] First backtest run completed (2026-05-19):
  - report: ML/training_30d.backtest_report.json
  - includes top-k ML vs heuristic comparison with deltas and relative lift
- [x] Server-side shadow-mode model loader integrated in recommendations.
- [x] Recommendation payload now emits ML fields without applying ML ranking:
  - mlEnabled
  - mlModelVersion
  - mlConfidenceTier
  - sellProb30d
  - expectedExecPrice30d
  - expectedValue30d
  - mlFallbackReason
- [x] Hybrid-ranking config gate integrated with safe default-off rollout.
- [x] Hybrid-ranking telemetry added:
  - fallback reason counters
  - confidence tier counters
  - hybrid applied/skip counters
  - ranking source per recommendation

### In Progress
- [ ] Finalize friction assumptions for EV calculation.
- [ ] Confirm confidence tier thresholds for sparse/medium/strong sales support.
- [ ] Review first-run training metrics and decide feature/algorithm iteration plan.

### Not Started

## Purpose
This file is the baseline reference for future work on ML-assisted recommendations.
It captures the agreed approach for thin markets where listing prices are often unreliable and realized sales should anchor valuation.

Use this file as the default source of truth when implementing or discussing ML in this repository.

## Problem Statement
In thin, low-movement markets, listing price is often an ask signal, not an execution signal.

Examples:
- Listed at 50 mirrors, only sale at 30 mirrors -> likely overvalued at 50.
- Listed at 8 mirrors, repeated sales at 6 mirrors -> sellable near 6, 8 is riskier.
- Listed at 2 mirrors, repeated sales at 2 mirrors -> likely correctly priced.

Implication:
- The model must treat sales as execution truth.
- Listings remain useful for supply and risk context, not as sole fair value.

## Primary Objectives
1. Improve recommendation quality versus heuristic-only ranking.
2. Penalize over-asking in thin markets.
3. Increase confidence when ask aligns with repeated executed sales.
4. Keep safe fallback behavior when ML confidence is low or model is unavailable.

## Scope (First Iteration)
Two-step predictive system per item variant at time t:

1. Classification:
- Predict P(sell within 30 days), named sellProb30d.

2. Execution valuation:
- Predict expected executable price in mirrors (expectedExecPrice30d).

3. Decision layer:
- Compute expected value:
  EV30 = sellProb30d * expectedExecPrice30d - entryPrice - friction

4. Ranking:
- Hybrid score initially (heuristic + ML), then tune with evidence.

## Definitions
- Ask price: current listing-derived entry price.
- Execution price: price level supported by realized sales.
- Fair value: shrinkage blend of sale anchor and listing anchor.
- Thin-market risk: high ask/sale gap + low sales support.

## Data Sources
SQLite source of truth: data/market.db

Tables used:
- item_polls
- poll_runs
- item_variants
- items
- listing_snapshots
- sales (non-reverted only)

Relevant code locations:
- storage/schema.py
- server/recommendation_service.py
- server/storage_service.py

## Dataset Row Design
One row per (item_variant_id, poll timestamp t).

Feature cutoff rule:
- Features may only use information available at or before t.

Label horizon:
- Labels may only use information in (t, t+30d].

## Dataset Contract v1 (Frozen 2026-05-19)
The first training contract is frozen to avoid feature drift during initial model training and backtesting.

Columns:
- item_poll_id
- item_variant_id
- requested_at_utc
- base_item_name
- display_name
- mode
- query_id
- entry_price_mirror
- listing_anchor_mirror
- sale_anchor_mirror
- fair_value_mirror
- ask_to_fair_gap_pct
- ask_to_sale_anchor_gap_pct
- sales_count_30d_past
- sales_count_90d_past
- days_since_last_sale
- acceptance_ratio_10pct
- acceptance_ratio_20pct
- total_results
- used_results
- used_results_raw
- used_exceeds_total_flag
- used_to_total_ratio
- inf_confirmed_transfer_30d
- inf_likely_instant_sale_30d
- inf_likely_non_instant_online_30d
- confirmed_share_of_signals
- sales_support_tier
- stale_price_flag
- y_sell_30d
- y_exec_price_30d

Contract notes:
- used_results is bounded to total_results for model-facing stability.
- used_results_raw preserves original source value for diagnostics.
- used_exceeds_total_flag marks rows where original used exceeded total.

## Labels (Targets)
1. y_sell_30d (classification)
- 1 if at least one non-reverted sale exists in (t, t+30d].
- 0 otherwise.

2. y_exec_price_30d (regression)
- Weighted median of non-reverted sale mirror_equiv in (t, t+30d].
- Null if no sale in horizon.

Optional later label:
- y_profitable_30d with a configurable minimum margin over entry price.

## Entry Price and Fair Value (Core to Thin-Market Handling)
Entry price at time t:
1. Prefer instant whole-mirror ladder floor from listing_snapshots.
2. Fallback to item_polls lowest/median mirror fields.

Sale anchor at time t:
- Weighted median of non-reverted sales in [t-90d, t], recency-weighted.

Listing anchor at time t:
- Robust median from recent listing-derived prices (not raw outliers).

Shrinkage blending:
- n = number of sales in last 90d
- k = smoothing constant (start with 5)
- w = n / (n + k)
- fairValue = w * saleAnchor + (1 - w) * listingAnchor

Interpretation:
- Sparse sales -> rely more on listing anchor.
- Strong sales history -> rely more on sale anchor.

## Feature Set (Minimum Baseline)
### A) Price alignment features
- entry_price_mirror
- fair_value_mirror
- sale_anchor_mirror
- ask_to_fair_gap_pct
- ask_to_sale_anchor_gap_pct

### B) Sales/liquidity features
- sales_count_30d_past
- sales_count_90d_past
- days_since_last_sale
- sale_price_iqr_90d
- acceptance_ratio_10pct (fraction of recent sales within +/-10% of entry price)
- acceptance_ratio_20pct (same with +/-20%)

### C) Listing/supply features
- total_results
- used_results
- floor_stock_count
- ladder_gap_to_next_pct
- listing_depth_top_n

### D) Trend/volatility features
- trend_7d
- trend_30d
- volatility_30d (robust, e.g., MAD or IQR-based)

### E) Inference quality features
- inf_confirmed_transfer_30d
- inf_likely_instant_sale_30d
- inf_likely_non_instant_online_30d
- confirmed_share_of_signals

### F) Confidence/support features
- sales_support_tier (sparse/medium/strong)
- stale_price_flag
- truncation_risk_flag

## Thin-Market Penalty Rules (Business Constraints)
Apply as model features and/or post-model guardrails.

Rule 1: Severe over-ask guard
- If ask_to_fair_gap_pct > 40% and sales_count_30d_past <= 1, strong penalty or block safe-category recommendation.

Rule 2: Weak evidence cap
- If no sales in 90d and low floor depth, cap score/confidence.

Rule 3: Price alignment boost
- If multiple recent sales within +/-10% of entry, increase confidence and reduce penalty.

## Model Plan (Baseline)
Stage 1 (safe baseline):
- Classifier: logistic regression or gradient boosting for y_sell_30d.
- Regressor: robust regression (or quantile model) on sold rows only for y_exec_price_30d.

Training split:
- Time-based split only (no random split).
- Example: oldest 70% train, next 15% validation, newest 15% test.

Metrics:
- Classification: PR-AUC, ROC-AUC, precision@k, recall@k, calibration.
- Regression: MAE/MAPE on sold rows.
- Business: top-k EV and realized outcomes vs heuristic baseline.

## Online Scoring Logic
At recommendation time for each candidate:
1. Build feature vector from current snapshot.
2. pSell = classifier probability.
3. pExec = regressor prediction (fallback fairValue).
4. EV30 = pSell * pExec - entryPrice - friction.
5. mlScore = normalized EV30.
6. finalScore = alpha * heuristicScore + (1 - alpha) * mlScore.

Fallback behavior:
- If model missing, feature mismatch, or low confidence tier, use heuristic-only score.

## API Additions (Shadow Mode First)
Add to recommendation payload:
- mlEnabled
- mlModelVersion
- mlConfidenceTier
- sellProb30d
- expectedExecPrice30d
- expectedValue30d
- mlFallbackReason (if disabled)

## Rollout Strategy
Phase A: Offline backtest only.
Phase B: Shadow mode (emit ML fields, do not affect ranking).
Phase C: Hybrid ranking with conservative alpha (example alpha=0.8 heuristic).
Phase D: Tune alpha only after sustained metric improvement.

## Acceptance Criteria (Initial)
1. Top-5 precision improves at least 10% over heuristic baseline on newest test window.
2. No major downside tail degradation.
3. Calibration is acceptable for sellProb30d.
4. Fallback path remains stable and non-breaking.

## Implementation Artifacts (Planned)
- ML/build_training_dataset.py
- ML/train_model.py
- ML/backtest_recommendations.py
- ML/ml_service.py
- server/recommendation_service.py (integration points)

## Non-Goals (First Iteration)
- Fully autonomous trading decisions.
- Replacing heuristic logic entirely.
- Multi-horizon multi-model optimization at launch.

## Open Questions to Resolve Before Build
1. Friction model in EV:
- Fixed spread/slippage cost or item-dependent cost?

2. Minimum sale count for "strong" confidence:
- Suggested initial tiers: sparse (0-1), medium (2-4), strong (5+)

3. Horizon selection:
- Start with 30d only, then test 14d and 60d extensions.

4. Label policy for uncertain inference:
- Should weak inference-only sales be down-weighted in labels?

## Operating Notes for Future Copilot Sessions
When asked to continue ML work, treat this file as baseline requirements unless the user explicitly overrides.

Preferred order for new work:
1. Build dataset and validate leakage rules.
2. Train/evaluate baseline models.
3. Add shadow-mode serving fields.
4. Gate hybrid ranking behind config.

If uncertainty exists, prioritize thin-market safeguards and conservative fallback behavior.
