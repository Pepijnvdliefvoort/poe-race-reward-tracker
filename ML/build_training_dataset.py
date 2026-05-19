from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = ROOT_DIR / "data" / "market.db"
DEFAULT_OUT_CSV = ROOT_DIR / "ML" / "training_30d.csv"
DEFAULT_OUT_META = ROOT_DIR / "ML" / "training_30d.meta.json"


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


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _safe_positive(value: Any) -> float | None:
    f = _safe_float(value)
    if f is None or f <= 0:
        return None
    return f


def _weighted_median(values: list[tuple[float, float]]) -> float | None:
    """Return weighted median of (value, weight) pairs."""
    clean = [(v, w) for v, w in values if w > 0 and math.isfinite(v) and math.isfinite(w)]
    if not clean:
        return None
    clean.sort(key=lambda x: x[0])
    total_weight = sum(w for _v, w in clean)
    threshold = total_weight / 2.0
    acc = 0.0
    for v, w in clean:
        acc += w
        if acc >= threshold:
            return v
    return clean[-1][0]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _ratio(numer: float | None, denom: float | None) -> float | None:
    if numer is None or denom is None or denom == 0:
        return None
    return numer / denom


def _gap_pct(current: float | None, anchor: float | None) -> float | None:
    if current is None or anchor is None or anchor <= 0:
        return None
    return (current - anchor) / anchor


@dataclass(frozen=True)
class SaleRow:
    occurred_at: datetime
    mirror_equiv: float


@dataclass(frozen=True)
class PollRow:
    item_poll_id: int
    item_variant_id: int
    base_item_name: str
    display_name: str
    mode: str
    requested_at: datetime
    query_id: str
    total_results: int
    used_results: int
    lowest_mirror: float | None
    median_mirror: float | None
    highest_mirror: float | None
    inf_confirmed_transfer: int
    inf_likely_instant_sale: int
    inf_likely_non_instant_online: int


class DatasetBuilder:
    def __init__(self, con: sqlite3.Connection, *, horizon_days: int = 30, anchor_days: int = 90) -> None:
        self._con = con
        self._horizon = timedelta(days=horizon_days)
        self._anchor_window = timedelta(days=anchor_days)

    def load_polls(self) -> list[PollRow]:
        rows = self._con.execute(
            """
            SELECT
              ip.id AS item_poll_id,
              ip.item_variant_id,
              i.name AS base_item_name,
              v.display_name,
              v.mode,
              ip.requested_at_utc,
              ip.query_id,
              ip.total_results,
              ip.used_results,
              ip.lowest_mirror,
              ip.median_mirror,
              ip.highest_mirror,
              ip.inf_confirmed_transfer,
              ip.inf_likely_instant_sale,
              ip.inf_likely_non_instant_online
            FROM item_polls ip
            JOIN item_variants v ON v.id = ip.item_variant_id
            JOIN items i ON i.id = v.item_id
            ORDER BY ip.item_variant_id ASC, ip.requested_at_utc ASC
            """
        ).fetchall()

        out: list[PollRow] = []
        for r in rows:
            dt = _parse_iso(r["requested_at_utc"])
            if dt is None:
                continue
            out.append(
                PollRow(
                    item_poll_id=int(r["item_poll_id"]),
                    item_variant_id=int(r["item_variant_id"]),
                    base_item_name=str(r["base_item_name"] or ""),
                    display_name=str(r["display_name"] or ""),
                    mode=str(r["mode"] or ""),
                    requested_at=dt,
                    query_id=str(r["query_id"] or ""),
                    total_results=int(r["total_results"] or 0),
                    used_results=int(r["used_results"] or 0),
                    lowest_mirror=_safe_positive(r["lowest_mirror"]),
                    median_mirror=_safe_positive(r["median_mirror"]),
                    highest_mirror=_safe_positive(r["highest_mirror"]),
                    inf_confirmed_transfer=int(r["inf_confirmed_transfer"] or 0),
                    inf_likely_instant_sale=int(r["inf_likely_instant_sale"] or 0),
                    inf_likely_non_instant_online=int(r["inf_likely_non_instant_online"] or 0),
                )
            )
        return out

    def load_sales_by_variant(self) -> dict[int, list[SaleRow]]:
        rows = self._con.execute(
            """
            SELECT
              item_variant_id,
              occurred_at_utc,
              mirror_equiv
            FROM sales
            WHERE reverted_at_utc IS NULL
              AND mirror_equiv IS NOT NULL
              AND mirror_equiv > 0
            ORDER BY item_variant_id ASC, occurred_at_utc ASC
            """
        ).fetchall()

        out: dict[int, list[SaleRow]] = {}
        for r in rows:
            dt = _parse_iso(r["occurred_at_utc"])
            m = _safe_positive(r["mirror_equiv"])
            if dt is None or m is None:
                continue
            vid = int(r["item_variant_id"])
            out.setdefault(vid, []).append(SaleRow(occurred_at=dt, mirror_equiv=m))
        return out

    def build_rows(self) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
        polls = self.load_polls()
        sales_by_variant = self.load_sales_by_variant()

        # Track historical entry prices for listing anchor per variant.
        history_entry_price: dict[int, list[tuple[datetime, float]]] = {}

        # Track rolling inference counts from historical poll rows (<= t only).
        history_inference: dict[int, list[tuple[datetime, int, int, int]]] = {}

        csv_rows: list[dict[str, Any]] = []

        for poll in polls:
            variant_sales = sales_by_variant.get(poll.item_variant_id, [])
            sale_times = [s.occurred_at for s in variant_sales]

            t = poll.requested_at
            t_minus_30 = t - timedelta(days=30)
            t_minus_90 = t - self._anchor_window
            t_plus_h = t + self._horizon

            # Entry price preference: low -> median -> high
            entry_price = poll.lowest_mirror or poll.median_mirror or poll.highest_mirror

            # Listing anchor from recent historical entry prices (<= t, last 30d).
            recent_listing_prices: list[float] = []
            for dt, p in history_entry_price.get(poll.item_variant_id, []):
                if dt < t_minus_30:
                    continue
                if dt <= t:
                    recent_listing_prices.append(p)
            listing_anchor = _median(recent_listing_prices)

            # Past sales windows.
            i_right = bisect_right(sale_times, t)
            past_sales = variant_sales[:i_right]
            past_sales_30 = [s for s in past_sales if s.occurred_at >= t_minus_30]
            past_sales_90 = [s for s in past_sales if s.occurred_at >= t_minus_90]

            # Recency-weighted past sale anchor (half-life 30d).
            weighted_past_sales: list[tuple[float, float]] = []
            for s in past_sales_90:
                age_days = max(0.0, (t - s.occurred_at).total_seconds() / 86400.0)
                weight = 0.5 ** (age_days / 30.0)
                weighted_past_sales.append((s.mirror_equiv, weight))
            sale_anchor = _weighted_median(weighted_past_sales)

            # Fair value shrinkage toward listing anchor when sale support is sparse.
            n_sales_90 = len(past_sales_90)
            k = 5.0
            w = n_sales_90 / (n_sales_90 + k)
            if sale_anchor is not None and listing_anchor is not None:
                fair_value = w * sale_anchor + (1.0 - w) * listing_anchor
            elif sale_anchor is not None:
                fair_value = sale_anchor
            else:
                fair_value = listing_anchor

            # Future labels from (t, t+30d]
            i_future_left = i_right
            i_future_right = bisect_right(sale_times, t_plus_h)
            future_sales = variant_sales[i_future_left:i_future_right]
            y_sell_30d = 1 if future_sales else 0

            weighted_future_sales: list[tuple[float, float]] = []
            for s in future_sales:
                future_days = max(0.0, (s.occurred_at - t).total_seconds() / 86400.0)
                weight = 0.5 ** (future_days / 30.0)
                weighted_future_sales.append((s.mirror_equiv, weight))
            y_exec_price_30d = _weighted_median(weighted_future_sales)

            days_since_last_sale = None
            if past_sales:
                days_since_last_sale = (t - past_sales[-1].occurred_at).total_seconds() / 86400.0

            def _acceptance_ratio(window_sales: list[SaleRow], band_pct: float) -> float | None:
                if entry_price is None or not window_sales:
                    return None
                lo = entry_price * (1.0 - band_pct)
                hi = entry_price * (1.0 + band_pct)
                matched = sum(1 for s in window_sales if lo <= s.mirror_equiv <= hi)
                return matched / len(window_sales)

            acceptance_10 = _acceptance_ratio(past_sales_90, 0.10)
            acceptance_20 = _acceptance_ratio(past_sales_90, 0.20)

            # Inference feature rollups from poll history up to t (last 30d).
            inf_rows = history_inference.get(poll.item_variant_id, [])
            inf_confirmed_30d = 0
            inf_instant_30d = 0
            inf_noninstant_online_30d = 0
            for dt, c, i, n in inf_rows:
                if dt < t_minus_30:
                    continue
                if dt <= t:
                    inf_confirmed_30d += c
                    inf_instant_30d += i
                    inf_noninstant_online_30d += n

            signal_total = inf_confirmed_30d + inf_instant_30d + inf_noninstant_online_30d
            confirmed_share = (inf_confirmed_30d / signal_total) if signal_total > 0 else None

            if n_sales_90 <= 1:
                sales_support_tier = "sparse"
            elif n_sales_90 <= 4:
                sales_support_tier = "medium"
            else:
                sales_support_tier = "strong"

            # Keep raw listing counts for diagnostics, and derive bounded values for model features.
            total_results_raw = poll.total_results
            used_results_raw = poll.used_results
            total_results = max(0, total_results_raw)
            used_results = max(0, used_results_raw)
            used_exceeds_total_flag = 1 if used_results > total_results else 0
            used_results_bounded = min(used_results, total_results)

            row: dict[str, Any] = {
                "item_poll_id": poll.item_poll_id,
                "item_variant_id": poll.item_variant_id,
                "requested_at_utc": t.isoformat(),
                "base_item_name": poll.base_item_name,
                "display_name": poll.display_name,
                "mode": poll.mode,
                "query_id": poll.query_id,
                "entry_price_mirror": entry_price,
                "listing_anchor_mirror": listing_anchor,
                "sale_anchor_mirror": sale_anchor,
                "fair_value_mirror": fair_value,
                "ask_to_fair_gap_pct": _gap_pct(entry_price, fair_value),
                "ask_to_sale_anchor_gap_pct": _gap_pct(entry_price, sale_anchor),
                "sales_count_30d_past": len(past_sales_30),
                "sales_count_90d_past": n_sales_90,
                "days_since_last_sale": days_since_last_sale,
                "acceptance_ratio_10pct": acceptance_10,
                "acceptance_ratio_20pct": acceptance_20,
                "total_results": total_results,
                "used_results": used_results_bounded,
                "used_results_raw": used_results_raw,
                "used_exceeds_total_flag": used_exceeds_total_flag,
                "used_to_total_ratio": _ratio(float(used_results_bounded), float(total_results) if total_results > 0 else None),
                "inf_confirmed_transfer_30d": inf_confirmed_30d,
                "inf_likely_instant_sale_30d": inf_instant_30d,
                "inf_likely_non_instant_online_30d": inf_noninstant_online_30d,
                "confirmed_share_of_signals": confirmed_share,
                "sales_support_tier": sales_support_tier,
                "stale_price_flag": 1 if entry_price is None else 0,
                "y_sell_30d": y_sell_30d,
                "y_exec_price_30d": y_exec_price_30d,
            }
            csv_rows.append(row)

            # Update feature histories AFTER row creation (prevents leakage).
            if entry_price is not None:
                history_entry_price.setdefault(poll.item_variant_id, []).append((t, entry_price))
            history_inference.setdefault(poll.item_variant_id, []).append(
                (
                    t,
                    poll.inf_confirmed_transfer,
                    poll.inf_likely_instant_sale,
                    poll.inf_likely_non_instant_online,
                )
            )

        columns = list(csv_rows[0].keys()) if csv_rows else []
        meta = {
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "dbPath": str(DB_PATH),
            "rows": len(csv_rows),
            "horizonDays": int(self._horizon.total_seconds() // 86400),
            "saleAnchorDays": int(self._anchor_window.total_seconds() // 86400),
            "columns": columns,
            "notes": [
                "Features are computed from data <= t only.",
                "Labels are computed from sales in (t, t+horizon].",
                "y_exec_price_30d is null when no sale in horizon.",
                "Fair value uses sale/listing shrinkage with k=5.",
            ],
        }
        return columns, csv_rows, meta


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python ML/build_training_dataset.py",
        description="Build thin-market ML training dataset from SQLite market history.",
    )
    parser.add_argument("--db", default=str(DB_PATH), help="Path to SQLite DB (default: data/market.db)")
    parser.add_argument("--out-csv", default=str(DEFAULT_OUT_CSV), help="Output dataset CSV path")
    parser.add_argument("--out-meta", default=str(DEFAULT_OUT_META), help="Output metadata JSON path")
    parser.add_argument("--horizon-days", type=int, default=30, help="Future label horizon in days")
    parser.add_argument("--sale-anchor-days", type=int, default=90, help="Past window for sale anchor in days")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    db_path = Path(args.db)
    out_csv = Path(args.out_csv)
    out_meta = Path(args.out_meta)
    horizon_days = max(1, int(args.horizon_days))
    sale_anchor_days = max(1, int(args.sale_anchor_days))

    if not db_path.is_file():
        raise SystemExit(f"DB not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        builder = DatasetBuilder(con, horizon_days=horizon_days, anchor_days=sale_anchor_days)
        columns, rows, meta = builder.build_rows()
    finally:
        con.close()

    _write_csv(out_csv, columns, rows)
    _write_json(out_meta, meta)

    print(f"Wrote dataset CSV: {out_csv}")
    print(f"Wrote metadata JSON: {out_meta}")
    print(f"Rows: {len(rows)}")


if __name__ == "__main__":
    main()
