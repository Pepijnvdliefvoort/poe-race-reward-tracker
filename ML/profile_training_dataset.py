from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT_DIR / "ML" / "training_30d.csv"
DEFAULT_OUT_JSON = ROOT_DIR / "ML" / "training_30d.profile.json"


def _to_float(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        f = float(raw)
    except ValueError:
        return None
    if not math.isfinite(f):
        return None
    return f


def _quantiles(values: list[float], probs: list[float]) -> dict[str, float | None]:
    if not values:
        return {f"p{int(p * 100)}": None for p in probs}
    s = sorted(values)
    n = len(s)
    out: dict[str, float | None] = {}
    for p in probs:
        if n == 1:
            out[f"p{int(p * 100)}"] = s[0]
            continue
        pos = p * (n - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            out[f"p{int(p * 100)}"] = s[lo]
        else:
            frac = pos - lo
            out[f"p{int(p * 100)}"] = s[lo] * (1.0 - frac) + s[hi] * frac
    return out


def profile_csv(csv_path: Path) -> dict[str, Any]:
    numeric_focus = [
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
        "used_to_total_ratio",
        "inf_confirmed_transfer_30d",
        "inf_likely_instant_sale_30d",
        "inf_likely_non_instant_online_30d",
        "confirmed_share_of_signals",
        "y_sell_30d",
        "y_exec_price_30d",
    ]

    rows = 0
    fields: list[str] = []
    null_counts: dict[str, int] = {}
    numeric_values: dict[str, list[float]] = {k: [] for k in numeric_focus}

    y_sell_counts = {"0": 0, "1": 0, "other": 0, "null": 0}
    y_exec_null_when_sell1 = 0
    y_exec_present_when_sell0 = 0

    acceptance_oob = {"acceptance_ratio_10pct": 0, "acceptance_ratio_20pct": 0}
    count_negative = {
        "sales_count_30d_past": 0,
        "sales_count_90d_past": 0,
        "total_results": 0,
        "used_results": 0,
    }
    used_gt_total = 0
    used_exceeds_total_flag_count = 0

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        null_counts = {f: 0 for f in fields}

        for row in reader:
            rows += 1

            for f in fields:
                raw = str(row.get(f, "") or "").strip()
                if raw == "":
                    null_counts[f] += 1

            # Numeric collections
            for col in numeric_focus:
                val = _to_float(row.get(col))
                if val is not None:
                    numeric_values[col].append(val)

            y_sell_raw = str(row.get("y_sell_30d", "") or "").strip()
            if y_sell_raw == "":
                y_sell_counts["null"] += 1
                y_sell = None
            elif y_sell_raw in {"0", "1"}:
                y_sell_counts[y_sell_raw] += 1
                y_sell = int(y_sell_raw)
            else:
                y_sell_counts["other"] += 1
                y_sell = None

            y_exec = _to_float(row.get("y_exec_price_30d"))
            if y_sell == 1 and y_exec is None:
                y_exec_null_when_sell1 += 1
            if y_sell == 0 and y_exec is not None:
                y_exec_present_when_sell0 += 1

            for col in ("acceptance_ratio_10pct", "acceptance_ratio_20pct"):
                v = _to_float(row.get(col))
                if v is not None and (v < 0.0 or v > 1.0):
                    acceptance_oob[col] += 1

            for col in count_negative:
                v = _to_float(row.get(col))
                if v is not None and v < 0:
                    count_negative[col] += 1

            used = _to_float(row.get("used_results"))
            total = _to_float(row.get("total_results"))
            if used is not None and total is not None and used > total:
                used_gt_total += 1

            flag = _to_float(row.get("used_exceeds_total_flag"))
            if flag is not None and flag > 0:
                used_exceeds_total_flag_count += 1

    null_rates = {
        f: (null_counts[f] / rows if rows > 0 else None)
        for f in fields
    }

    numeric_summary: dict[str, Any] = {}
    for col in numeric_focus:
        vals = numeric_values[col]
        if not vals:
            numeric_summary[col] = {
                "count": 0,
                "min": None,
                "max": None,
                "mean": None,
                "std": None,
                "p25": None,
                "p50": None,
                "p75": None,
                "p95": None,
            }
            continue

        q = _quantiles(vals, [0.25, 0.50, 0.75, 0.95])
        numeric_summary[col] = {
            "count": len(vals),
            "min": min(vals),
            "max": max(vals),
            "mean": mean(vals),
            "std": pstdev(vals) if len(vals) > 1 else 0.0,
            "p25": q["p25"],
            "p50": q["p50"],
            "p75": q["p75"],
            "p95": q["p95"],
        }

    y_total_non_null = y_sell_counts["0"] + y_sell_counts["1"]
    y_sell_rate = (y_sell_counts["1"] / y_total_non_null) if y_total_non_null else None

    top_null_fields = sorted(
        ({"column": f, "nullRate": r} for f, r in null_rates.items()),
        key=lambda x: (x["nullRate"] if x["nullRate"] is not None else -1),
        reverse=True,
    )[:10]

    return {
        "csvPath": str(csv_path),
        "rowCount": rows,
        "columnCount": len(fields),
        "columns": fields,
        "targets": {
            "y_sell_30d": {
                "counts": y_sell_counts,
                "sellRate": y_sell_rate,
            },
            "y_exec_price_30d": {
                "nullWhenSell1": y_exec_null_when_sell1,
                "presentWhenSell0": y_exec_present_when_sell0,
            },
        },
        "sanityChecks": {
            "acceptanceRatioOutOfBounds": acceptance_oob,
            "negativeCounts": count_negative,
            "usedGreaterThanTotal": used_gt_total,
            "usedExceedsTotalFlagCount": used_exceeds_total_flag_count,
        },
        "topNullRateColumns": top_null_fields,
        "numericSummary": numeric_summary,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python ML/profile_training_dataset.py",
        description="Run quality/profile checks on ML training CSV.",
    )
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to training CSV")
    parser.add_argument("--out-json", default=str(DEFAULT_OUT_JSON), help="Path to output profile JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv)
    out_json = Path(args.out_json)

    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    profile = profile_csv(csv_path)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)

    targets = profile["targets"]
    checks = profile["sanityChecks"]

    print(f"Profile written: {out_json}")
    print(f"Rows: {profile['rowCount']}")
    print(
        "y_sell_30d counts: "
        f"0={targets['y_sell_30d']['counts']['0']}, "
        f"1={targets['y_sell_30d']['counts']['1']}, "
        f"null={targets['y_sell_30d']['counts']['null']}, "
        f"other={targets['y_sell_30d']['counts']['other']}"
    )
    print(f"y_sell_30d sellRate: {targets['y_sell_30d']['sellRate']}")
    print(
        "y_exec consistency: "
        f"nullWhenSell1={targets['y_exec_price_30d']['nullWhenSell1']}, "
        f"presentWhenSell0={targets['y_exec_price_30d']['presentWhenSell0']}"
    )
    print(
        "sanity checks: "
        f"usedGreaterThanTotal={checks['usedGreaterThanTotal']}, "
        f"usedExceedsTotalFlagCount={checks['usedExceedsTotalFlagCount']}, "
        f"acceptanceRatioOutOfBounds={checks['acceptanceRatioOutOfBounds']}"
    )


if __name__ == "__main__":
    main()
