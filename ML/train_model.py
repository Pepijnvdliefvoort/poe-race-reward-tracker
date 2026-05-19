from __future__ import annotations

import argparse
import csv
import json
import math
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import HuberRegressor, LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    mean_absolute_error,
    mean_absolute_percentage_error,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT_DIR / "ML" / "training_30d.csv"
DEFAULT_CLASSIFIER_OUT = ROOT_DIR / "ML" / "model_sellprob_30d.pkl"
DEFAULT_REGRESSOR_OUT = ROOT_DIR / "ML" / "model_execprice_30d.pkl"
DEFAULT_REPORT_OUT = ROOT_DIR / "ML" / "training_30d.training_report.json"

NUMERIC_FEATURES = [
    "entry_price_mirror",
    "listing_anchor_mirror",
    "sale_anchor_mirror",
    "fair_value_mirror",
    "ask_to_fair_gap_pct",
    "ask_to_sale_anchor_gap_pct",
    "sales_count_30d_past",
    "sales_count_90d_past",
    "days_since_last_sale",
    "acceptance_ratio_10pct",
    "acceptance_ratio_20pct",
    "total_results",
    "used_results",
    "used_results_raw",
    "used_exceeds_total_flag",
    "used_to_total_ratio",
    "inf_confirmed_transfer_30d",
    "inf_likely_instant_sale_30d",
    "inf_likely_non_instant_online_30d",
    "confirmed_share_of_signals",
    "stale_price_flag",
]

TIER_MAP = {
    "sparse": 0.0,
    "medium": 1.0,
    "strong": 2.0,
}


@dataclass(frozen=True)
class Row:
    requested_at: datetime
    features: list[float]
    y_sell: int
    y_exec: float | None


def _to_float(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if raw == "":
        return None
    try:
        f = float(raw)
    except ValueError:
        return None
    if not math.isfinite(f):
        return None
    return f


def _parse_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_feature_vector(raw: dict[str, str]) -> list[float]:
    values: list[float] = []
    for col in NUMERIC_FEATURES:
        v = _to_float(raw.get(col))
        values.append(v if v is not None else float("nan"))

    tier = str(raw.get("sales_support_tier", "") or "").strip().lower()
    values.append(TIER_MAP.get(tier, float("nan")))
    return values


def _load_rows(csv_path: Path) -> list[Row]:
    out: list[Row] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            requested_at = _parse_iso(raw.get("requested_at_utc"))
            if requested_at is None:
                continue

            y_sell_raw = str(raw.get("y_sell_30d", "") or "").strip()
            if y_sell_raw not in {"0", "1"}:
                continue
            y_sell = int(y_sell_raw)

            y_exec = _to_float(raw.get("y_exec_price_30d"))
            out.append(
                Row(
                    requested_at=requested_at,
                    features=_build_feature_vector(raw),
                    y_sell=y_sell,
                    y_exec=y_exec,
                )
            )

    out.sort(key=lambda r: r.requested_at)
    return out


def _split_indices(n: int, train_frac: float, valid_frac: float) -> tuple[int, int]:
    train_end = int(n * train_frac)
    valid_end = int(n * (train_frac + valid_frac))
    train_end = max(1, min(train_end, n - 2))
    valid_end = max(train_end + 1, min(valid_end, n - 1))
    return train_end, valid_end


def _classification_metrics(y_true: np.ndarray, y_prob: np.ndarray) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "prAuc": None,
        "rocAuc": None,
        "brier": None,
    }

    classes = np.unique(y_true)
    if classes.size >= 2:
        out["prAuc"] = float(average_precision_score(y_true, y_prob))
        out["rocAuc"] = float(roc_auc_score(y_true, y_prob))
        out["brier"] = float(brier_score_loss(y_true, y_prob))
    return out


def _calibration_bins(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> list[dict[str, float | int]]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    out: list[dict[str, float | int]] = []
    for i in range(bins):
        lo = edges[i]
        hi = edges[i + 1]
        if i == bins - 1:
            mask = (y_prob >= lo) & (y_prob <= hi)
        else:
            mask = (y_prob >= lo) & (y_prob < hi)
        idx = np.where(mask)[0]
        if idx.size == 0:
            continue
        prob_mean = float(np.mean(y_prob[idx]))
        actual_rate = float(np.mean(y_true[idx]))
        out.append(
            {
                "binStart": float(lo),
                "binEnd": float(hi),
                "count": int(idx.size),
                "predictedMean": prob_mean,
                "actualRate": actual_rate,
            }
        )
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python ML/train_model.py",
        description="Train baseline sell-probability and execution-price models with time split.",
    )
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Input training CSV path")
    parser.add_argument("--out-classifier", default=str(DEFAULT_CLASSIFIER_OUT), help="Classifier output .pkl")
    parser.add_argument("--out-regressor", default=str(DEFAULT_REGRESSOR_OUT), help="Regressor output .pkl")
    parser.add_argument("--out-report", default=str(DEFAULT_REPORT_OUT), help="Training report output JSON")
    parser.add_argument("--train-frac", type=float, default=0.70, help="Train fraction")
    parser.add_argument("--valid-frac", type=float, default=0.15, help="Validation fraction")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    csv_path = Path(args.csv)
    out_classifier = Path(args.out_classifier)
    out_regressor = Path(args.out_regressor)
    out_report = Path(args.out_report)

    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    rows = _load_rows(csv_path)
    if len(rows) < 100:
        raise SystemExit("Not enough rows to train models.")

    X = np.array([r.features for r in rows], dtype=float)
    y_sell = np.array([r.y_sell for r in rows], dtype=int)
    y_exec = np.array([np.nan if r.y_exec is None else r.y_exec for r in rows], dtype=float)

    train_end, valid_end = _split_indices(len(rows), args.train_frac, args.valid_frac)

    X_train = X[:train_end]
    X_valid = X[train_end:valid_end]
    X_test = X[valid_end:]

    y_train = y_sell[:train_end]
    y_valid = y_sell[train_end:valid_end]
    y_test = y_sell[valid_end:]

    classifier = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    max_iter=2000,
                    class_weight="balanced",
                    solver="lbfgs",
                    random_state=42,
                ),
            ),
        ]
    )
    classifier.fit(X_train, y_train)

    y_prob_valid = classifier.predict_proba(X_valid)[:, 1]
    y_prob_test = classifier.predict_proba(X_test)[:, 1]

    cls_metrics_valid = _classification_metrics(y_valid, y_prob_valid)
    cls_metrics_test = _classification_metrics(y_test, y_prob_test)

    reg_train_mask = ~np.isnan(y_exec[:train_end])
    reg_valid_mask = ~np.isnan(y_exec[train_end:valid_end])
    reg_test_mask = ~np.isnan(y_exec[valid_end:])

    regressor = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", HuberRegressor()),
        ]
    )

    if np.sum(reg_train_mask) < 50:
        raise SystemExit("Not enough sold rows to train regressor.")

    regressor.fit(X_train[reg_train_mask], y_exec[:train_end][reg_train_mask])

    reg_valid_pred = regressor.predict(X_valid[reg_valid_mask]) if np.any(reg_valid_mask) else np.array([])
    reg_test_pred = regressor.predict(X_test[reg_test_mask]) if np.any(reg_test_mask) else np.array([])

    reg_valid_true = y_exec[train_end:valid_end][reg_valid_mask]
    reg_test_true = y_exec[valid_end:][reg_test_mask]

    reg_metrics_valid = {
        "mae": float(mean_absolute_error(reg_valid_true, reg_valid_pred)) if reg_valid_true.size else None,
        "mape": float(mean_absolute_percentage_error(reg_valid_true, reg_valid_pred)) if reg_valid_true.size else None,
    }
    reg_metrics_test = {
        "mae": float(mean_absolute_error(reg_test_true, reg_test_pred)) if reg_test_true.size else None,
        "mape": float(mean_absolute_percentage_error(reg_test_true, reg_test_pred)) if reg_test_true.size else None,
    }

    out_classifier.parent.mkdir(parents=True, exist_ok=True)
    out_regressor.parent.mkdir(parents=True, exist_ok=True)
    out_report.parent.mkdir(parents=True, exist_ok=True)

    with out_classifier.open("wb") as fh:
        pickle.dump(classifier, fh)
    with out_regressor.open("wb") as fh:
        pickle.dump(regressor, fh)

    report: dict[str, Any] = {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "csvPath": str(csv_path),
            "rows": int(len(rows)),
            "featureCount": int(X.shape[1]),
            "trainRows": int(train_end),
            "validRows": int(valid_end - train_end),
            "testRows": int(len(rows) - valid_end),
            "soldTrainRows": int(np.sum(reg_train_mask)),
            "soldValidRows": int(np.sum(reg_valid_mask)),
            "soldTestRows": int(np.sum(reg_test_mask)),
        },
        "models": {
            "classifier": {
                "algorithm": "LogisticRegression",
                "outputPath": str(out_classifier),
                "valid": cls_metrics_valid,
                "test": cls_metrics_test,
                "testCalibrationBins": _calibration_bins(y_test, y_prob_test, bins=10),
            },
            "regressor": {
                "algorithm": "HuberRegressor",
                "outputPath": str(out_regressor),
                "valid": reg_metrics_valid,
                "test": reg_metrics_test,
            },
        },
        "featureSchema": {
            "numeric": NUMERIC_FEATURES,
            "derived": ["sales_support_tier_ordinal"],
        },
    }

    with out_report.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print(f"Trained classifier -> {out_classifier}")
    print(f"Trained regressor -> {out_regressor}")
    print(f"Wrote report -> {out_report}")
    print(
        "Classifier test: "
        f"PR-AUC={report['models']['classifier']['test']['prAuc']}, "
        f"ROC-AUC={report['models']['classifier']['test']['rocAuc']}, "
        f"Brier={report['models']['classifier']['test']['brier']}"
    )
    print(
        "Regressor test: "
        f"MAE={report['models']['regressor']['test']['mae']}, "
        f"MAPE={report['models']['regressor']['test']['mape']}"
    )


if __name__ == "__main__":
    main()
