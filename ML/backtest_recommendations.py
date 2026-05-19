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


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT_DIR / "ML" / "training_30d.csv"
DEFAULT_CLASSIFIER = ROOT_DIR / "ML" / "model_sellprob_30d.pkl"
DEFAULT_REGRESSOR = ROOT_DIR / "ML" / "model_execprice_30d.pkl"
DEFAULT_REPORT = ROOT_DIR / "ML" / "training_30d.backtest_report.json"

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
    item_poll_id: int
    entry_price: float | None
    fair_value: float | None
    y_sell: int
    y_exec: float | None
    features: list[float]


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


def _split_indices(n: int, train_frac: float, valid_frac: float) -> tuple[int, int]:
    train_end = int(n * train_frac)
    valid_end = int(n * (train_frac + valid_frac))
    train_end = max(1, min(train_end, n - 2))
    valid_end = max(train_end + 1, min(valid_end, n - 1))
    return train_end, valid_end


def _build_feature_vector(raw: dict[str, str]) -> list[float]:
    vals: list[float] = []
    for col in NUMERIC_FEATURES:
        v = _to_float(raw.get(col))
        vals.append(v if v is not None else float("nan"))
    tier = str(raw.get("sales_support_tier", "") or "").strip().lower()
    vals.append(TIER_MAP.get(tier, float("nan")))
    return vals


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

            y_exec = _to_float(raw.get("y_exec_price_30d"))
            out.append(
                Row(
                    requested_at=requested_at,
                    item_poll_id=int(_to_float(raw.get("item_poll_id")) or 0),
                    entry_price=_to_float(raw.get("entry_price_mirror")),
                    fair_value=_to_float(raw.get("fair_value_mirror")),
                    y_sell=int(y_sell_raw),
                    y_exec=y_exec,
                    features=_build_feature_vector(raw),
                )
            )
    out.sort(key=lambda r: r.requested_at)
    return out


def _safe_ev(p_sell: float, p_exec: float | None, entry_price: float | None, friction: float) -> float:
    if entry_price is None:
        return -1e12
    exec_price = p_exec if p_exec is not None and math.isfinite(p_exec) else entry_price
    return (p_sell * exec_price) - entry_price - friction


def _safe_heuristic(entry_price: float | None, fair_value: float | None, friction: float) -> float:
    if entry_price is None:
        return -1e12
    anchor = fair_value if fair_value is not None and math.isfinite(fair_value) else entry_price
    return anchor - entry_price - friction


def _realized_edge(entry_price: float | None, y_sell: int, y_exec: float | None, friction: float) -> float | None:
    if entry_price is None:
        return None
    if y_sell == 1 and y_exec is not None and math.isfinite(y_exec):
        return y_exec - entry_price - friction
    return -entry_price - friction


def _evaluate_topk(rows: list[Row], scores: np.ndarray, friction: float, k_values: list[int]) -> dict[str, Any]:
    order = np.argsort(-scores)
    y_sell = np.array([r.y_sell for r in rows], dtype=int)

    out: dict[str, Any] = {}
    for k in k_values:
        k_eff = min(k, len(rows))
        if k_eff <= 0:
            continue
        idx = order[:k_eff]

        selected_rows = [rows[i] for i in idx]
        sell_rate = float(np.mean(y_sell[idx]))

        realized_edges = [
            _realized_edge(r.entry_price, r.y_sell, r.y_exec, friction)
            for r in selected_rows
        ]
        realized_edges_clean = [x for x in realized_edges if x is not None]
        sold_edges = [
            _realized_edge(r.entry_price, r.y_sell, r.y_exec, friction)
            for r in selected_rows
            if r.y_sell == 1
        ]
        sold_edges_clean = [x for x in sold_edges if x is not None]

        out[str(k)] = {
            "evaluatedK": int(k_eff),
            "sellRate": sell_rate,
            "avgRealizedEdgeAll": float(np.mean(realized_edges_clean)) if realized_edges_clean else None,
            "avgRealizedEdgeSoldOnly": float(np.mean(sold_edges_clean)) if sold_edges_clean else None,
        }
    return out


def _lift(base: float | None, challenger: float | None) -> float | None:
    if base is None or challenger is None or abs(base) < 1e-12:
        return None
    return (challenger - base) / abs(base)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python ML/backtest_recommendations.py",
        description="Backtest ML EV ranking against heuristic ranking on test split.",
    )
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Input dataset CSV")
    parser.add_argument("--classifier", default=str(DEFAULT_CLASSIFIER), help="Classifier .pkl path")
    parser.add_argument("--regressor", default=str(DEFAULT_REGRESSOR), help="Regressor .pkl path")
    parser.add_argument("--out-report", default=str(DEFAULT_REPORT), help="Output report JSON")
    parser.add_argument("--friction", type=float, default=0.0, help="Fixed friction cost in mirrors")
    parser.add_argument("--train-frac", type=float, default=0.70, help="Train fraction")
    parser.add_argument("--valid-frac", type=float, default=0.15, help="Validation fraction")
    parser.add_argument(
        "--k-values",
        default="5,10,20,50,100",
        help="Comma-separated top-k list for ranking evaluation",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    csv_path = Path(args.csv)
    classifier_path = Path(args.classifier)
    regressor_path = Path(args.regressor)
    out_report = Path(args.out_report)

    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")
    if not classifier_path.is_file():
        raise SystemExit(f"Classifier not found: {classifier_path}")
    if not regressor_path.is_file():
        raise SystemExit(f"Regressor not found: {regressor_path}")

    rows = _load_rows(csv_path)
    if len(rows) < 100:
        raise SystemExit("Not enough rows to run backtest.")

    with classifier_path.open("rb") as fh:
        classifier = pickle.load(fh)
    with regressor_path.open("rb") as fh:
        regressor = pickle.load(fh)

    train_end, valid_end = _split_indices(len(rows), args.train_frac, args.valid_frac)
    test_rows = rows[valid_end:]
    if not test_rows:
        raise SystemExit("No test rows available after split.")

    X_test = np.array([r.features for r in test_rows], dtype=float)

    p_sell = classifier.predict_proba(X_test)[:, 1]
    p_exec = regressor.predict(X_test)

    friction = float(args.friction)
    k_values = [int(x) for x in str(args.k_values).split(",") if str(x).strip()]

    ml_scores = np.array(
        [_safe_ev(float(ps), float(pe), r.entry_price, friction) for r, ps, pe in zip(test_rows, p_sell, p_exec)],
        dtype=float,
    )
    heuristic_scores = np.array(
        [_safe_heuristic(r.entry_price, r.fair_value, friction) for r in test_rows],
        dtype=float,
    )

    ml_topk = _evaluate_topk(test_rows, ml_scores, friction, k_values)
    heuristic_topk = _evaluate_topk(test_rows, heuristic_scores, friction, k_values)

    comparison: dict[str, Any] = {}
    for k in ml_topk:
        base = heuristic_topk.get(k, {})
        chal = ml_topk[k]
        sell_base = base.get("sellRate")
        sell_chal = chal.get("sellRate")
        edge_all_base = base.get("avgRealizedEdgeAll")
        edge_all_chal = chal.get("avgRealizedEdgeAll")
        edge_sold_base = base.get("avgRealizedEdgeSoldOnly")
        edge_sold_chal = chal.get("avgRealizedEdgeSoldOnly")

        comparison[k] = {
            "sellRateDelta": (sell_chal - sell_base) if sell_base is not None and sell_chal is not None else None,
            "sellRateLift": _lift(sell_base, sell_chal),
            "avgRealizedEdgeAllDelta": (
                edge_all_chal - edge_all_base
                if edge_all_base is not None and edge_all_chal is not None
                else None
            ),
            "avgRealizedEdgeAllLift": _lift(edge_all_base, edge_all_chal),
            "avgRealizedEdgeSoldOnlyDelta": (
                edge_sold_chal - edge_sold_base
                if edge_sold_base is not None and edge_sold_chal is not None
                else None
            ),
            "avgRealizedEdgeSoldOnlyLift": _lift(edge_sold_base, edge_sold_chal),
        }

    report: dict[str, Any] = {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "csvPath": str(csv_path),
            "rows": int(len(rows)),
            "testRows": int(len(test_rows)),
            "trainFrac": float(args.train_frac),
            "validFrac": float(args.valid_frac),
            "friction": friction,
        },
        "models": {
            "classifierPath": str(classifier_path),
            "regressorPath": str(regressor_path),
        },
        "ranking": {
            "mlTopK": ml_topk,
            "heuristicTopK": heuristic_topk,
            "lift": comparison,
        },
        "notes": [
            "Heuristic score uses fair_value_mirror - entry_price_mirror - friction.",
            "ML score uses pSell * pExec - entry_price_mirror - friction.",
            "avgRealizedEdgeAll treats non-sold rows as -entry_price - friction.",
        ],
    }

    out_report.parent.mkdir(parents=True, exist_ok=True)
    with out_report.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print(f"Wrote backtest report -> {out_report}")
    for k in k_values:
        key = str(k)
        if key not in comparison:
            continue
        print(
            f"K={k} lifts: "
            f"sellRateDelta={comparison[key]['sellRateDelta']}, "
            f"sellRateLift={comparison[key]['sellRateLift']}, "
            f"realizedAllDelta={comparison[key]['avgRealizedEdgeAllDelta']}, "
            f"realizedAllLift={comparison[key]['avgRealizedEdgeAllLift']}, "
            f"realizedSoldOnlyDelta={comparison[key]['avgRealizedEdgeSoldOnlyDelta']}, "
            f"realizedSoldOnlyLift={comparison[key]['avgRealizedEdgeSoldOnlyLift']}"
        )


if __name__ == "__main__":
    main()
