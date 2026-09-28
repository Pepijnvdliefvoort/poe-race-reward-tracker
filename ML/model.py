"""
Learned return-per-day model.

Target: the realized return per day of the simulated trade (buy the floor, list at the
estimator's ask), measured only where the full horizon is observed. Inputs: the point-in-time
features plus the estimator's own outputs, so the model learns *corrections* to the formula.

The model is only used for ranking when walk-forward evaluation shows it beats the estimator.
"""

from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ML.estimator import EstimatorParams, estimate
from ML.features import DAY, FEATURE_NAMES, Snapshot
from ML.market import Market
from ML.simulate import realize_estimate

MODEL_DIR_NAME = "models"
MODEL_FILE = "profit_model.pkl"
META_FILE = "profit_model.json"

MODEL_FEATURES: list[str] = FEATURE_NAMES + [
    "est_return_per_day",
    "est_sell_probability",
    "est_return_if_sold",
    "est_log_days",
    "est_queue_ahead",
    "est_log1p_similar_listing_days",
    "est_similar_listings_sold",
]

# Realized return/day is heavy-tailed (a +20% sale after 3 hours is 160%/day). Train on a tempered,
# clipped version so a handful of lucky fast sales can't dominate the fit.
LABEL_MIN_DAYS = 1.0
LABEL_CLIP = (-0.05, 0.25)
MIN_TRAIN_ROWS = 150
MIN_TRAIN_TIMES = 3


def feature_vector(snap: Snapshot, params: EstimatorParams) -> list[float]:
    est = estimate(snap, params)
    row = snap.as_feature_row()
    row.update(
        {
            "est_return_per_day": est.return_per_day,
            "est_sell_probability": est.sell_probability,
            "est_return_if_sold": est.return_if_sold,
            "est_log_days": math.log(max(est.expected_days, 1e-3)),
            "est_queue_ahead": float(est.queue_ahead),
            "est_log1p_similar_listing_days": math.log1p(est.similar_listing_days_90d),
            "est_similar_listings_sold": float(est.similar_listings_sold_90d),
        }
    )
    return [row[name] for name in MODEL_FEATURES]


@dataclass(frozen=True)
class LabeledRow:
    ts: float
    variant_id: int
    features: list[float]
    label: float


def label_rows(market: Market, snaps_by_time: dict[float, list[Snapshot]], params: EstimatorParams) -> list[LabeledRow]:
    """One row per (decision time, variant) whose horizon is fully inside the data."""
    rows: list[LabeledRow] = []
    for ts, snaps in snaps_by_time.items():
        if market.end_ts is None or ts + params.horizon_days * DAY > market.end_ts:
            continue
        for snap in snaps:
            outcome = realize_estimate(market.variants[snap.variant_id], snap, params)
            y = outcome.ret / max(outcome.days, LABEL_MIN_DAYS)
            rows.append(LabeledRow(ts, snap.variant_id, feature_vector(snap, params), min(max(y, LABEL_CLIP[0]), LABEL_CLIP[1])))
    return rows


def new_regressor():
    from sklearn.ensemble import HistGradientBoostingRegressor

    # Small, heavily regularized trees: the dataset is ~a hundred items x a few dozen weeks.
    return HistGradientBoostingRegressor(
        loss="absolute_error",
        max_depth=3,
        max_iter=200,
        learning_rate=0.05,
        min_samples_leaf=20,
        l2_regularization=1.0,
        random_state=42,
    )


def fit(rows: list[LabeledRow]):
    import numpy as np

    model = new_regressor()
    model.fit(np.array([r.features for r in rows], dtype=float), np.array([r.label for r in rows], dtype=float))
    return model


def predict(model, snaps: list[Snapshot], params: EstimatorParams) -> dict[int, float]:
    import numpy as np

    if not snaps:
        return {}
    X = np.array([feature_vector(s, params) for s in snaps], dtype=float)
    return {s.variant_id: float(p) for s, p in zip(snaps, model.predict(X))}


class WalkForwardScorer:
    """
    Ranks with a model refit at every decision time on rows whose outcomes were already known
    at that time (label window ended before the decision). Falls back to the estimator while
    there is not enough history; `active_times` records where the model was really used.
    """

    def __init__(self, rows: list[LabeledRow], params: EstimatorParams) -> None:
        self._rows = rows
        self._params = params
        self.active_times: set[float] = set()

    def __call__(self, snaps: list[Snapshot]) -> dict[int, float]:
        ts = snaps[0].ts
        cutoff = ts - self._params.horizon_days * DAY
        train = [r for r in self._rows if r.ts <= cutoff]
        if len(train) < MIN_TRAIN_ROWS or len({r.ts for r in train}) < MIN_TRAIN_TIMES:
            return {s.variant_id: estimate(s, self._params).return_per_day for s in snaps}
        self.active_times.add(ts)
        return predict(fit(train), snaps, self._params)


# --- persistence ---------------------------------------------------------------------------


def model_dir(root_dir: Path) -> Path:
    return Path(root_dir) / "ML" / MODEL_DIR_NAME


def save(root_dir: Path, model, meta: dict[str, Any]) -> None:
    d = model_dir(root_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp_model = d / (MODEL_FILE + ".tmp")
    tmp_meta = d / (META_FILE + ".tmp")
    with tmp_model.open("wb") as fh:
        pickle.dump(model, fh)
    tmp_meta.write_text(json.dumps(meta, indent=2, allow_nan=False), encoding="utf-8")
    # Model first, then metadata: the server keys its cache on the metadata file.
    tmp_model.replace(d / MODEL_FILE)
    tmp_meta.replace(d / META_FILE)


def save_meta_only(root_dir: Path, meta: dict[str, Any]) -> None:
    d = model_dir(root_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (META_FILE + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(d / META_FILE)


def sklearn_version() -> str | None:
    try:
        import sklearn

        return str(sklearn.__version__)
    except Exception:
        return None


_CACHE: dict[str, Any] = {"key": None, "model": None, "meta": None, "reason": None}


def load_for_serving(root_dir: Path) -> tuple[Any | None, dict[str, Any] | None, str | None]:
    """
    Returns (model, meta, reason_if_unused). The model is only returned when the last retrain
    enabled it (it beat the estimator) and it was trained with the installed scikit-learn.
    """
    meta_path = model_dir(root_dir) / META_FILE
    model_path = model_dir(root_dir) / MODEL_FILE
    if not meta_path.is_file():
        return None, None, "no-model-trained-yet"
    try:
        key = (str(meta_path), meta_path.stat().st_mtime_ns, model_path.stat().st_mtime_ns if model_path.is_file() else None)
    except OSError:
        return None, None, "model-files-unreadable"
    if _CACHE["key"] == key:
        return _CACHE["model"], _CACHE["meta"], _CACHE["reason"]

    model = None
    meta: dict[str, Any] | None = None
    reason: str | None = None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        meta, reason = None, "model-metadata-unreadable"
    if meta is not None:
        if not meta.get("enabled"):
            reason = str(meta.get("disabledReason") or "model-did-not-beat-estimator")
        elif meta.get("sklearnVersion") != sklearn_version():
            reason = f"model-trained-with-sklearn-{meta.get('sklearnVersion')}"
        elif list(meta.get("features") or []) != MODEL_FEATURES:
            reason = "model-feature-mismatch"
        elif not model_path.is_file():
            reason = "model-file-missing"
        else:
            try:
                with model_path.open("rb") as fh:
                    model = pickle.load(fh)
            except Exception as exc:  # noqa: BLE001
                model, reason = None, f"model-load-failed: {exc}"
    _CACHE.update({"key": key, "model": model, "meta": meta, "reason": reason})
    return model, meta, reason


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
