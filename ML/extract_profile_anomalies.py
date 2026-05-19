from __future__ import annotations

import argparse
import csv
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT_DIR / "ML" / "training_30d.csv"
DEFAULT_OUT = ROOT_DIR / "ML" / "training_30d.anomalies_used_gt_total.csv"


OUTPUT_COLUMNS = [
    "item_poll_id",
    "item_variant_id",
    "requested_at_utc",
    "base_item_name",
    "display_name",
    "mode",
    "query_id",
    "entry_price_mirror",
    "sale_anchor_mirror",
    "fair_value_mirror",
    "ask_to_fair_gap_pct",
    "ask_to_sale_anchor_gap_pct",
    "sales_support_tier",
    "total_results",
    "used_results",
    "used_results_raw",
    "used_exceeds_total_flag",
    "used_to_total_ratio",
    "y_sell_30d",
    "y_exec_price_30d",
]


def _to_float(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _is_anomaly(row: dict[str, str]) -> bool:
    flag = _to_float(row.get("used_exceeds_total_flag"))
    if flag is not None and flag > 0:
        return True

    used_raw = _to_float(row.get("used_results_raw"))
    total = _to_float(row.get("total_results"))
    if used_raw is not None and total is not None and used_raw > total:
        return True

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python ML/extract_profile_anomalies.py",
        description="Export anomaly rows from training dataset for focused review.",
    )
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to training CSV")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Path to anomaly output CSV")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv)
    out_path = Path(args.out)

    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    anomaly_rows = 0

    with csv_path.open("r", encoding="utf-8", newline="") as in_fh, out_path.open(
        "w", encoding="utf-8", newline=""
    ) as out_fh:
        reader = csv.DictReader(in_fh)
        writer = csv.DictWriter(out_fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()

        for row in reader:
            total_rows += 1
            if not _is_anomaly(row):
                continue
            anomaly_rows += 1
            writer.writerow({k: row.get(k, "") for k in OUTPUT_COLUMNS})

    print(f"Scanned rows: {total_rows}")
    print(f"Anomaly rows exported: {anomaly_rows}")
    print(f"Output CSV: {out_path}")


if __name__ == "__main__":
    main()
