# ML-Assisted Ranking: First Implementation Task List

This document turns the feature plan into a concrete first build sequence mapped to current code locations.

## Scope Of First Iteration
- Predict: probability of profitable flip within 30 days (`profitProb30d`)
- Model type: simple baseline (logistic regression)
- Serving mode: hybrid rank (heuristic + ML)
- Safety: full fallback to heuristic-only if model or features are unavailable

## Phase 1: Dataset Builder (Offline)

### Task 1.1: Create a training data export script
- New file: `tools/ml/build_training_dataset.py`
- Purpose: read SQLite market history, generate feature rows and labels for `N=30`.

Data sources to join:
- `item_polls`, `poll_runs`, `item_variants`, `items`
- `listing_snapshots` (for ladder depth/spread)
- `sales` (for outcome labeling)

Reference schema:
- `storage/schema.py`

Output format:
- CSV (easy inspection) and optional JSON metadata
- Example output path: `data/ml/training_30d.csv`

### Task 1.2: Implement strict anti-leakage checks
- In dataset builder, enforce:
  - feature timestamp = poll snapshot time
  - label window = `(t, t+30d]`
  - no columns derived from future snapshots inside features

### Task 1.3: Define baseline features
Start with features already available in current recommendation logic:
- current/last-known price
- `total_results`, `used_results`
- inferred sale counts over recent windows
- trend over recent window
- instant ladder summary from `listing_snapshots` (floor stock, gap to next listing)

Touchpoint for feature parity:
- `server/recommendation_service.py`

## Phase 2: Model Training (Offline)

### Task 2.1: Add ML dependencies
- Update `requirements.txt`:
  - `scikit-learn`
  - `joblib`
  - (optional) `numpy`, `pandas` if needed for faster iteration

### Task 2.2: Create train/eval script
- New file: `tools/ml/train_model.py`
- Inputs: `data/ml/training_30d.csv`
- Split: time-based train/validation/test
- Train baseline logistic regression
- Save artifact:
  - `data/ml/models/profit_30d_model.joblib`
  - `data/ml/models/profit_30d_model.meta.json`

### Task 2.3: Print benchmark report
At minimum report:
- ROC-AUC
- PR-AUC
- precision@k (`k=3,5,8`)
- calibration buckets
- heuristic-vs-ML top-k comparison

## Phase 3: Server Integration (Online Inference)

### Task 3.1: Add a lightweight model loader
- New file: `server/ml_service.py`
- Responsibilities:
  - load model artifact + metadata at startup/lazy first request
  - validate feature schema/version
  - expose `predict_profit_probability(feature_row)`
  - fail closed to `None` (never crash recommendation API)

### Task 3.2: Compute serving features from existing recommendation pipeline
- Main touchpoint: `server/recommendation_service.py`
- Add feature assembly per candidate item right before score/rank.
- Reuse existing helpers where possible (trend, demand, ladder, etc.).

### Task 3.3: Blend scores and expose ML fields
In recommendation payload add:
- `profitProb30d`
- `mlEnabled`
- `mlModelVersion`
- optional `mlReason` when fallback is used

Rank logic first pass:
- `hybrid = 0.5 * heuristic + 0.5 * ml_prob`
- Keep configurable in constants or app config for easy tuning.

## Phase 4: API, Config, and Rollout Safety

### Task 4.1: Add config flags (safe rollout)
Suggested keys in app config:
- `ml.enabled` (default false)
- `ml.weight` (default 0.5)
- `ml.model_path`
- `ml.min_confidence` (optional)

Storage/config touchpoint:
- `storage/schema.py` (`app_config` table already exists)

### Task 4.2: Keep hard fallback
If any of the following occurs:
- model missing
- model load error
- feature mismatch
- prediction failure

Then use current heuristic ranking unchanged.

### Task 4.3: Log model usage for observability
Touchpoints:
- `server/structured_logging.py`
- recommendation flow in `server/recommendation_service.py`

Log fields:
- model version
- ml enabled/disabled reason
- prediction latency
- hybrid weight

## Phase 5: Validation and Backtesting

### Task 5.1: Add a reproducible backtest runner
- New file: `tools/ml/backtest_recommendations.py`
- Replay historical slices and compare:
  - heuristic-only top-k
  - ML-only top-k
  - hybrid top-k

Use your existing TODO direction for backtesting windows:
- 30/60/90 day analyses

### Task 5.2: Define go/no-go thresholds
Example first thresholds:
- hybrid precision@5 >= heuristic precision@5 + 10%
- no major degradation in downside tails
- stable calibration (no major overconfidence)

## Suggested Initial File Touchpoints
- `requirements.txt`
- `server/recommendation_service.py`
- `storage/schema.py` (config usage; likely no migration needed for first pass)
- `server/structured_logging.py`
- `tools/ml/build_training_dataset.py` (new)
- `tools/ml/train_model.py` (new)
- `tools/ml/backtest_recommendations.py` (new)
- `server/ml_service.py` (new)

## First PR Plan (Small, Mergeable)
PR 1:
- dataset export script
- train script
- saved example artifact
- no runtime integration yet

PR 2:
- model loader service
- recommendation payload fields (without ranking influence)
- fallback + logs

PR 3:
- hybrid ranking enabled behind config flag
- backtest script + benchmark report

## Expected First-Iteration Result
- Investment companion still behaves safely under failures.
- API can provide a learned probability signal per candidate.
- Ranking quality should become more consistent than manually weighted heuristics alone.
- You have a clear path to retraining and iterative improvement.
