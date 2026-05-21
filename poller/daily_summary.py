"""Daily recap: aggregate SQLite stats, render charts, post to Discord."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from env_loader import load_local_env
from storage.service import StorageService

_DAILY_SUMMARY_WEBHOOK_ENV_KEYS = (
    "DISCORD_WEBHOOK_URL_DAILY_SUMMARY",
    "POE_DISCORD_WEBHOOK_URL_DAILY_SUMMARY",
    "DISCORD_WEBHOOK_URL",
    "POE_DISCORD_WEBHOOK_URL",
)


def resolve_daily_summary_webhook_url(*, cfg_url: str = "") -> str:
    """
    Resolve the daily-recap webhook from config, then repo ``.env`` files, then os.environ.
    """
    url = (cfg_url or "").strip()
    if url:
        return url
    load_local_env()
    for key in _DAILY_SUMMARY_WEBHOOK_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _tz_from_offset_minutes(offset_minutes: int) -> timezone:
    return timezone(timedelta(minutes=int(offset_minutes)))


def _fmt_int(value: float) -> str:
    return str(int(round(float(value))))


def _fmt_amount(value: float) -> str:
    """One decimal place only when the tenths digit is non-zero (e.g. 20.5, not 20.0)."""
    v = round(float(value), 1)
    whole = round(v)
    if abs(v - whole) < 1e-9:
        return str(int(whole))
    return f"{v:.1f}"


def _fmt_mirrors(value: float) -> str:
    return f"{_fmt_amount(value)} mirrors"


def _fmt_pct(value: float) -> str:
    return f"{int(round(float(value))):+d}%"


def _fmt_time_ampm(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {dt.strftime('%p')}"


@dataclass(frozen=True)
class DailySummaryConfig:
    enabled: bool = True
    webhook_url: str = ""
    tz_offset_minutes: int = 120  # GMT+2 default
    schedule_hour: int = 8
    schedule_minute: int = 0
    top_items_limit: int = 8


@dataclass(frozen=True)
class SummaryPeriod:
    label: str
    start_utc: datetime
    end_utc: datetime


def _period_last_24_hours(*, end_utc: datetime | None = None) -> SummaryPeriod:
    """Rolling window: the 24 hours ending at ``end_utc`` (default: now)."""
    end = end_utc or _utc_now()
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(hours=24)
    label = (
        f"{start.strftime('%b %d')} {_fmt_time_ampm(start)} – "
        f"{end.strftime('%b %d')} {_fmt_time_ampm(end)} UTC"
    )
    return SummaryPeriod(label=label, start_utc=start, end_utc=end)


def _iso_bounds(period: SummaryPeriod) -> tuple[str, str]:
    return (
        period.start_utc.isoformat(),
        period.end_utc.isoformat(),
    )


def _query_summary(storage: StorageService, period: SummaryPeriod) -> dict[str, Any]:
    start_iso, end_iso = _iso_bounds(period)
    con = sqlite3.connect(str(storage.db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        sales_totals = con.execute(
            """
            SELECT COUNT(*) AS cnt,
                   SUM(COALESCE(mirror_equiv, 0) * COALESCE(quantity, 1)) AS vol
            FROM sales
            WHERE occurred_at_utc >= ? AND occurred_at_utc < ?
              AND reverted_at_utc IS NULL
            """,
            (start_iso, end_iso),
        ).fetchone()

        top_items = con.execute(
            """
            SELECT COALESCE(iv.display_name, i.name) AS item_name,
                   COUNT(*) AS sale_cnt,
                   SUM(COALESCE(s.mirror_equiv, 0) * COALESCE(s.quantity, 1)) AS vol
            FROM sales s
            JOIN item_variants iv ON iv.id = s.item_variant_id
            JOIN items i ON i.id = iv.item_id
            WHERE s.occurred_at_utc >= ? AND s.occurred_at_utc < ?
              AND s.reverted_at_utc IS NULL
            GROUP BY s.item_variant_id
            ORDER BY vol DESC, sale_cnt DESC
            LIMIT 20
            """,
            (start_iso, end_iso),
        ).fetchall()

        largest_sale = con.execute(
            """
            SELECT COALESCE(iv.display_name, i.name) AS item_name,
                   s.mirror_equiv, s.rule, s.seller
            FROM sales s
            JOIN item_variants iv ON iv.id = s.item_variant_id
            JOIN items i ON i.id = iv.item_id
            WHERE s.occurred_at_utc >= ? AND s.occurred_at_utc < ?
              AND s.reverted_at_utc IS NULL
              AND s.mirror_equiv IS NOT NULL
            ORDER BY (s.mirror_equiv * COALESCE(s.quantity, 1)) DESC
            LIMIT 1
            """,
            (start_iso, end_iso),
        ).fetchone()

        reprice_rows = con.execute(
            """
            SELECT ip.requested_at_utc,
                   ie.prev_mirror_equiv,
                   ie.curr_mirror_equiv
            FROM inference_events ie
            JOIN item_polls ip ON ip.id = ie.item_poll_id
            WHERE ie.rule = 'reprice_same_seller'
              AND ip.requested_at_utc >= ? AND ip.requested_at_utc < ?
              AND ie.prev_mirror_equiv IS NOT NULL
              AND ie.curr_mirror_equiv IS NOT NULL
            """,
            (start_iso, end_iso),
        ).fetchall()

        hourly_sales_mirrors = con.execute(
            """
            SELECT strftime('%Y-%m-%d %H:00:00', occurred_at_utc) AS hour_bucket,
                   SUM(COALESCE(mirror_equiv, 0) * COALESCE(quantity, 1)) AS vol
            FROM sales
            WHERE occurred_at_utc >= ? AND occurred_at_utc < ?
              AND reverted_at_utc IS NULL
            GROUP BY hour_bucket
            ORDER BY hour_bucket
            """,
            (start_iso, end_iso),
        ).fetchall()

        floor_poll_rows = con.execute(
            """
            SELECT ip.item_variant_id,
                   COALESCE(iv.display_name, i.name) AS item_name,
                   ip.requested_at_utc,
                   ip.lowest_mirror
            FROM item_polls ip
            JOIN item_variants iv ON iv.id = ip.item_variant_id
            JOIN items i ON i.id = iv.item_id
            WHERE ip.requested_at_utc >= ? AND ip.requested_at_utc < ?
              AND ip.lowest_mirror IS NOT NULL
              AND ip.lowest_mirror > 0
            ORDER BY ip.item_variant_id, ip.requested_at_utc
            """,
            (start_iso, end_iso),
        ).fetchall()

    finally:
        con.close()

    total_sales = int(sales_totals["cnt"] if sales_totals else 0)
    total_volume = float(sales_totals["vol"] if sales_totals and sales_totals["vol"] is not None else 0.0)

    top_item_rows = [
        {
            "name": str(r["item_name"] or "?"),
            "count": int(r["sale_cnt"] or 0),
            "volume": float(r["vol"] or 0.0),
        }
        for r in top_items
    ]

    largest: dict[str, Any] | None = None
    if largest_sale and largest_sale["mirror_equiv"] is not None:
        largest = {
            "item_name": str(largest_sale["item_name"] or "?"),
            "mirror_equiv": float(largest_sale["mirror_equiv"]),
            "rule": str(largest_sale["rule"] or ""),
            "seller": str(largest_sale["seller"] or ""),
        }

    reprice = _aggregate_reprice_rows(reprice_rows)
    mirrors = _aggregate_sales_mirrors(
        sales_volume=total_volume,
        hourly_sales=hourly_sales_mirrors,
    )
    item_trends = _aggregate_item_floor_movers(floor_poll_rows)

    return {
        "period": period,
        "total_sales": total_sales,
        "total_volume": total_volume,
        "top_items": top_item_rows,
        "largest_sale": largest,
        "reprice": reprice,
        "mirrors": mirrors,
        "item_trends": item_trends,
    }


_REPRICE_PCT_EPSILON = 0.05  # ignore |% change| below this (float noise)
_ITEM_FLOOR_PCT_EPSILON = 1.0  # count up/down vs window-open floor


def _parse_utc_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hour_bucket_utc(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _hourly_sum_series(buckets: dict[datetime, float]) -> list[tuple[str, float]]:
    return [(bucket.isoformat(), float(buckets[bucket])) for bucket in sorted(buckets.keys())]


def _aggregate_reprice_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    hourly_up_count: dict[datetime, int] = {}
    hourly_down_count: dict[datetime, int] = {}
    up_count = 0
    down_count = 0
    flat_count = 0
    up_pct_values: list[float] = []
    down_pct_values: list[float] = []

    for row in rows:
        try:
            prev = float(row["prev_mirror_equiv"])
            curr = float(row["curr_mirror_equiv"])
        except (TypeError, ValueError):
            continue
        if prev <= 0 or not (prev == prev) or not (curr == curr):
            continue
        when = _parse_utc_iso(row["requested_at_utc"])
        if when is None:
            continue
        bucket = _hour_bucket_utc(when)
        pct = (curr - prev) / prev * 100.0
        if pct > _REPRICE_PCT_EPSILON:
            up_count += 1
            up_pct_values.append(pct)
            hourly_up_count[bucket] = hourly_up_count.get(bucket, 0) + 1
        elif pct < -_REPRICE_PCT_EPSILON:
            down_count += 1
            down_pct_values.append(abs(pct))
            hourly_down_count[bucket] = hourly_down_count.get(bucket, 0) + 1
        else:
            flat_count += 1

    return {
        "total": up_count + down_count + flat_count,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "avg_up_pct": sum(up_pct_values) / len(up_pct_values) if up_pct_values else None,
        "avg_down_pct": sum(down_pct_values) / len(down_pct_values) if down_pct_values else None,
        "series_up_count": _hourly_sum_series({k: float(v) for k, v in hourly_up_count.items()}),
        "series_down_count": _hourly_sum_series({k: float(v) for k, v in hourly_down_count.items()}),
    }


def _aggregate_sales_mirrors(
    *,
    sales_volume: float,
    hourly_sales: list[sqlite3.Row],
) -> dict[str, Any]:
    hourly_sales_vol: dict[datetime, float] = {}
    for row in hourly_sales:
        raw = str(row["hour_bucket"] or "").strip()
        when = _parse_utc_iso(raw) or _parse_utc_iso(f"{raw}+00:00" if raw and "+" not in raw else "")
        if when is None:
            continue
        bucket = _hour_bucket_utc(when)
        hourly_sales_vol[bucket] = hourly_sales_vol.get(bucket, 0.0) + float(row["vol"] or 0.0)

    return {
        "sales_mirrors": float(sales_volume),
        "series_hourly": _hourly_sum_series(hourly_sales_vol),
    }


def _aggregate_item_floor_movers(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Window-open floor (first poll) vs latest floor per tracked item."""
    open_floor: dict[int, float] = {}
    latest_floor: dict[int, float] = {}
    item_names: dict[int, str] = {}

    for row in rows:
        try:
            variant_id = int(row["item_variant_id"])
            floor = float(row["lowest_mirror"])
        except (TypeError, ValueError):
            continue
        if floor <= 0 or not (floor == floor):
            continue

        item_names[variant_id] = str(row["item_name"] or "?")
        if variant_id not in open_floor:
            open_floor[variant_id] = floor
        latest_floor[variant_id] = floor

    items_up = 0
    items_down = 0
    items_flat = 0
    movers: list[dict[str, Any]] = []

    for variant_id, first_low in open_floor.items():
        last_low = latest_floor.get(variant_id)
        if last_low is None or first_low <= 0:
            continue
        pct = (last_low - first_low) / first_low * 100.0
        if pct > _ITEM_FLOOR_PCT_EPSILON:
            items_up += 1
            direction = "up"
        elif pct < -_ITEM_FLOOR_PCT_EPSILON:
            items_down += 1
            direction = "down"
        else:
            items_flat += 1
            direction = "flat"
        movers.append(
            {
                "name": item_names.get(variant_id, "?"),
                "pct": pct,
                "first_low": first_low,
                "last_low": last_low,
                "direction": direction,
            }
        )

    movers.sort(key=lambda m: abs(float(m["pct"])), reverse=True)

    return {
        "tracked_items": len(open_floor),
        "items_up": items_up,
        "items_down": items_down,
        "items_flat": items_flat,
        "top_risers": [m for m in movers if m["direction"] == "up"][:5],
        "top_fallers": [m for m in movers if m["direction"] == "down"][:5],
    }


def _mirrors_cumulative_from_zero(
    period: SummaryPeriod,
    mirrors: dict[str, Any],
) -> tuple[list[datetime], list[float]]:
    """Cumulative sales volume; first point is always (window start, 0)."""
    hourly: list[tuple[datetime, float]] = []
    for iso, vol in mirrors.get("series_hourly") or []:
        dt = _parse_utc_iso(iso)
        if dt is None:
            continue
        hourly.append((dt, float(vol)))
    hourly.sort(key=lambda row: row[0])

    running = 0.0
    for dt, vol in hourly:
        if dt < period.start_utc:
            running += vol

    plot_times = [period.start_utc]
    plot_cum = [0.0]
    for dt, vol in hourly:
        if dt < period.start_utc:
            continue
        running += vol
        plot_times.append(dt)
        plot_cum.append(running)

    if len(plot_times) == 1:
        plot_times.append(period.end_utc)
        plot_cum.append(running)
    elif plot_times[-1] != period.end_utc:
        plot_times.append(period.end_utc)
        plot_cum.append(plot_cum[-1])
    return plot_times, plot_cum


def _rule_label(rule: str) -> str:
    if rule == "confirmed_transfer":
        return "Confirmed transfer"
    if rule == "likely_instant_sale":
        return "Likely instant sale"
    return rule.replace("_", " ").title()


# Dashboard theme (web/css/variables.css)
_DASH = {
    "bg": "#090c12",
    "surface": "#141d2c",
    "line": "#38485f",
    "ink": "#f0f6ff",
    "ink_soft": "#9eb2cc",
    "accent": "#ff7a2f",
    "accent2": "#2ab7bf",
    "ok": "#26b96d",
    "error": "#ff4d4f",
    "grid": "#1a2332",
}


def _import_plt() -> Any | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams.update(
            {
                "font.family": "sans-serif",
                "font.sans-serif": ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"],
                "axes.edgecolor": _DASH["line"],
                "axes.labelcolor": _DASH["ink_soft"],
                "text.color": _DASH["ink"],
                "xtick.color": _DASH["ink_soft"],
                "ytick.color": _DASH["ink_soft"],
                "grid.color": _DASH["grid"],
                "grid.alpha": 0.55,
            }
        )
        return plt
    except Exception:
        return None


def _series_to_points(raw: list[tuple[str, float]]) -> tuple[list[datetime], list[float]]:
    times: list[datetime] = []
    values: list[float] = []
    for iso, value in raw:
        dt = _parse_utc_iso(iso)
        if dt is None:
            continue
        times.append(dt)
        values.append(float(value))
    return times, values


def _new_chart(*, plt: Any, title: str, subtitle: str, figsize: tuple[float, float] = (10, 5.2)) -> tuple[Any, Any]:
    fig, ax = plt.subplots(figsize=figsize, facecolor=_DASH["bg"])
    fig.text(0.06, 0.96, title, color=_DASH["ink"], fontsize=15, fontweight="bold", va="top")
    if subtitle:
        fig.text(0.06, 0.91, subtitle, color=_DASH["ink_soft"], fontsize=10, va="top")
    ax.set_facecolor(_DASH["surface"])
    ax.grid(True, axis="y", linestyle="--", linewidth=0.6, alpha=0.65)
    for spine in ax.spines.values():
        spine.set_color(_DASH["line"])
    return fig, ax


def _window_time_ticks(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Evenly spaced ticks anchored to the window (always includes start and end)."""
    span_hours = max((end_utc - start_utc).total_seconds() / 3600.0, 0.0)
    if span_hours <= 0:
        return [start_utc]

    for step in (2, 3, 4, 6, 8, 12):
        n = int(span_hours // step) + 1
        if 4 <= n <= 8:
            step_hours = step
            break
    else:
        step_hours = max(1, int(round(span_hours / 5)))

    ticks = [start_utc]
    t = start_utc
    while True:
        t = t + timedelta(hours=step_hours)
        if t >= end_utc:
            break
        ticks.append(t)
    if ticks[-1] != end_utc:
        ticks.append(end_utc)
    return ticks


def _style_time_axis(
    ax: Any,
    fig: Any,
    plt: Any,
    *,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> None:
    ax.set_xlabel("Time (UTC)", color=_DASH["ink_soft"], fontsize=9, labelpad=8)
    if start_utc is not None and end_utc is not None:
        ticks = _window_time_ticks(start_utc, end_utc)
        ax.set_xlim(start_utc, end_utc)
        ax.set_xticks(ticks)
        ax.set_xticklabels([_fmt_time_ampm(t) for t in ticks], rotation=28, ha="right")


def _style_integer_axis(ax: Any, *, axis: str = "y") -> None:
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    locator = MaxNLocator(integer=True)
    formatter = FuncFormatter(lambda x, _pos: _fmt_int(x))
    if axis in ("x", "both"):
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)
    if axis in ("y", "both"):
        ax.yaxis.set_major_locator(locator)
        ax.yaxis.set_major_formatter(formatter)


def _style_amount_axis(ax: Any, *, axis: str = "y") -> None:
    from matplotlib.ticker import FuncFormatter

    formatter = FuncFormatter(lambda x, _pos: _fmt_amount(x))
    if axis in ("x", "both"):
        ax.xaxis.set_major_formatter(formatter)
    if axis in ("y", "both"):
        ax.yaxis.set_major_formatter(formatter)


def _empty_ax(ax: Any, message: str) -> None:
    ax.text(0.5, 0.5, message, ha="center", va="center", color=_DASH["ink_soft"], transform=ax.transAxes, fontsize=11)


def _save_figure(fig: Any, path: Path, plt: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140, facecolor=_DASH["bg"], edgecolor="none", bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)


def _chart_top_items(*, data: dict[str, Any], path: Path, plt: Any, top_n: int) -> None:
    period: SummaryPeriod = data["period"]
    top_items: list[dict[str, Any]] = data.get("top_items") or []
    shown = top_items[:top_n]

    fig, ax = _new_chart(
        plt=plt,
        title="Top items",
        subtitle=f"By est. mirror volume · {period.label}",
        figsize=(10, max(4.5, min(top_n, 10) * 0.5)),
    )
    if shown:
        names = [str(x["name"])[:36] for x in shown][::-1]
        vols = [float(x["volume"]) for x in shown][::-1]
        ymax = max(vols) if vols else 1.0
        colors = [_DASH["accent"] if i == 0 else _DASH["accent2"] for i in range(len(vols))]
        bars = ax.barh(names, vols, color=colors, height=0.62, edgecolor="none", alpha=0.92)
        ax.set_xlabel("Mirrors (est.)", color=_DASH["ink_soft"], fontsize=9)
        for bar, v in zip(bars, vols):
            ax.text(
                bar.get_width() + ymax * 0.02,
                bar.get_y() + bar.get_height() / 2,
                _fmt_mirrors(v),
                va="center",
                ha="left",
                color=_DASH["ink"],
                fontsize=8,
            )
        ax.set_xlim(0, ymax * 1.18 if ymax else 1)
        _style_amount_axis(ax, axis="x")
    else:
        _empty_ax(ax, "No sales in the last 24 hours")
    fig.subplots_adjust(left=0.28, right=0.97, top=0.84, bottom=0.10)
    _save_figure(fig, path, plt)


def _chart_reprice_trend(*, data: dict[str, Any], path: Path, plt: Any) -> None:
    period: SummaryPeriod = data["period"]
    reprice: dict[str, Any] = data.get("reprice") or {}
    up_times, up_counts = _series_to_points(reprice.get("series_up_count") or [])
    down_times, down_counts = _series_to_points(reprice.get("series_down_count") or [])
    up_n = int(reprice.get("up_count") or 0)
    down_n = int(reprice.get("down_count") or 0)
    avg_up = reprice.get("avg_up_pct")
    avg_down = reprice.get("avg_down_pct")
    sub = f"↑ {up_n} · ↓ {down_n}"
    if avg_up is not None:
        sub += f" · avg ↑ {_fmt_int(avg_up)}%"
    if avg_down is not None:
        sub += f" · avg ↓ {_fmt_int(avg_down)}%"

    fig, ax = _new_chart(plt=plt, title="Reprice activity", subtitle=f"{sub} · {period.label}")

    up_map = dict(zip(up_times, up_counts))
    down_map = dict(zip(down_times, down_counts))
    buckets = sorted(set(up_map.keys()) | set(down_map.keys()))
    if buckets:
        x = list(range(len(buckets)))
        width = 0.38
        up_vals = [up_map.get(b, 0) for b in buckets]
        down_vals = [down_map.get(b, 0) for b in buckets]
        ax.bar(
            [i - width / 2 for i in x],
            up_vals,
            width=width,
            color=_DASH["ok"],
            label="Price up",
            alpha=0.9,
            zorder=3,
        )
        ax.bar(
            [i + width / 2 for i in x],
            down_vals,
            width=width,
            color=_DASH["error"],
            label="Price down",
            alpha=0.9,
            zorder=3,
        )
        ymax = max([*up_vals, *down_vals], default=0)
        pad = max(1, int(round(ymax * 0.15)))
        ax.set_ylim(0, ymax + pad)
        ax.set_xticks(x, [_fmt_time_ampm(t) for t in buckets], rotation=28, ha="right", fontsize=8)
        ax.set_ylabel("Reprices / hour", color=_DASH["ink_soft"], fontsize=9)
        ax.legend(
            loc="upper right",
            frameon=True,
            facecolor=_DASH["surface"],
            edgecolor=_DASH["line"],
            labelcolor=_DASH["ink"],
            fontsize=9,
        )
        _style_integer_axis(ax, axis="y")
    else:
        _empty_ax(ax, "No reprices in the last 24 hours")
    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.18)
    _save_figure(fig, path, plt)


def _chart_mirrors_moved(*, data: dict[str, Any], path: Path, plt: Any) -> None:
    period: SummaryPeriod = data["period"]
    mirrors: dict[str, Any] = data.get("mirrors") or {}
    total = float(mirrors.get("sales_mirrors") or 0.0)
    times, cum = _mirrors_cumulative_from_zero(period, mirrors)
    has_sales = total > 0 or (len(cum) > 1 and max(cum) > 0)

    fig, ax = _new_chart(
        plt=plt,
        title="Mirrors moved (sales)",
        subtitle=f"{_fmt_mirrors(total)} cumulative est. volume · {period.label}",
    )
    if has_sales or len(times) >= 2:
        ymax = max(cum) if cum else 0.0
        y_top = max(ymax * 1.12, 1.0)
        ax.fill_between(times, cum, 0, color=_DASH["accent"], alpha=0.18, zorder=1)
        ax.plot(times, cum, color=_DASH["accent"], linewidth=2.6, marker="o", markersize=4, zorder=3)
        ax.set_ylabel("Cumulative mirrors", color=_DASH["ink_soft"], fontsize=9)
        _style_time_axis(ax, fig, plt, start_utc=period.start_utc, end_utc=period.end_utc)
        ax.set_ylim(0, y_top)
        _style_amount_axis(ax, axis="y")
        ax.margins(x=0)
    else:
        _empty_ax(ax, "No sales in the last 24 hours")
    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.14)
    _save_figure(fig, path, plt)


def _render_chart_images(*, data: dict[str, Any], exports_dir: Path, top_n: int) -> list[Path]:
    """Render each visualization as its own PNG; returns paths that were written."""
    plt = _import_plt()
    if plt is None:
        return []

    stamp = _utc_now().strftime("%Y%m%d-%H%M")
    prefix = exports_dir / f"market-summary-{stamp}"
    charts: list[tuple[str, Callable[[Path], None]]] = [
        ("01-top-items", lambda p: _chart_top_items(data=data, path=p, plt=plt, top_n=top_n)),
        ("02-reprice-trend", lambda p: _chart_reprice_trend(data=data, path=p, plt=plt)),
        ("03-mirrors-moved", lambda p: _chart_mirrors_moved(data=data, path=p, plt=plt)),
    ]

    written: list[Path] = []
    for slug, draw in charts:
        path = Path(f"{prefix}-{slug}.png")
        try:
            draw(path)
            if path.is_file():
                written.append(path)
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
    return written


def _build_embed(*, data: dict[str, Any]) -> dict[str, Any]:
    period: SummaryPeriod = data["period"]
    total_volume = float(data["total_volume"])
    total_sales = int(data["total_sales"])
    mirrors: dict[str, Any] = data.get("mirrors") or {}
    item_trends: dict[str, Any] = data.get("item_trends") or {}
    reprice: dict[str, Any] = data.get("reprice") or {}

    lines = [
        f"**Window:** last 24 hours ({period.label})",
        f"**Est. sales:** {_fmt_mirrors(total_volume)} ({total_sales} sale{'s' if total_sales != 1 else ''})",
        f"**Mirrors moved (sales):** {_fmt_mirrors(float(mirrors.get('sales_mirrors') or 0))}",
    ]

    if int(reprice.get("total") or 0) > 0:
        reprice_line = f"**Reprices:** {reprice.get('up_count', 0)} up / {reprice.get('down_count', 0)} down"
        if reprice.get("avg_up_pct") is not None:
            reprice_line += f" (avg ↑ {_fmt_int(reprice['avg_up_pct'])}%)"
        if reprice.get("avg_down_pct") is not None:
            reprice_line += f" (avg ↓ {_fmt_int(reprice['avg_down_pct'])}%)"
        lines.append(reprice_line)

    top_items: list[dict[str, Any]] = data.get("top_items") or []
    if top_items:
        lines.append("")
        lines.append("**Top items:**")
        for row in top_items[:5]:
            lines.append(
                f"• {row['name']}: {_fmt_mirrors(float(row['volume']))} ({int(row['count'])} sale{'s' if int(row['count']) != 1 else ''})"
            )

    for label, key in (("Biggest risers", "top_risers"), ("Biggest fallers", "top_fallers")):
        rows = item_trends.get(key) or []
        if rows:
            lines.append("")
            lines.append(f"**{label}:**")
            for row in rows[:3]:
                lines.append(
                    f"• {row['name']}: {_fmt_pct(row['pct'])} "
                    f"({_fmt_mirrors(float(row['first_low']))} → {_fmt_mirrors(float(row['last_low']))})"
                )

    return {
        "title": "Daily recap (24h)",
        "description": "\n".join(lines)[:4096],
        "color": 0xFF7A2F,
        "footer": {"text": "poe-market-flips"},
        "timestamp": period.end_utc.isoformat(),
    }


_CHART_SLUG_TITLES: dict[str, str] = {
    "01-top-items": "Top items",
    "02-reprice-trend": "Reprice activity",
    "03-mirrors-moved": "Mirrors moved (sales)",
}


def _chart_message_content(path: Path) -> str:
    name = path.name
    for slug, title in _CHART_SLUG_TITLES.items():
        if slug in name:
            return f"**{title}**"
    return ""


def _forum_thread_name(*, period: SummaryPeriod) -> str:
    """Discord thread_name limit is 100 characters."""
    end = period.end_utc
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return f"Daily recap · {end.strftime('%Y-%m-%d')}"[:100]


def _webhook_execute_url(
    webhook_url: str,
    *,
    thread_id: str | None = None,
    wait: bool = False,
) -> str:
    parsed = urlparse(webhook_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if thread_id:
        query["thread_id"] = thread_id
    if wait:
        query["wait"] = "true"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _thread_id_from_webhook_message_response(resp: requests.Response) -> str | None:
    """Forum thread id is the ``channel_id`` on the starter message when ``wait=true``."""
    if not resp.ok:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    channel_id = data.get("channel_id")
    if channel_id is None:
        return None
    return str(channel_id)


def _discord_webhook_post_message(
    *,
    webhook_url: str,
    content: str = "",
    embed: dict[str, Any] | None = None,
    image_path: Path | None = None,
    thread_name: str | None = None,
    thread_id: str | None = None,
    wait: bool = False,
    timeout: float,
) -> requests.Response:
    """Post one Discord webhook message (optionally with a single image attachment)."""
    url = _webhook_execute_url(webhook_url, thread_id=thread_id, wait=wait)
    payload: dict[str, Any] = {}
    if embed is not None:
        payload["embeds"] = [embed]
    if content.strip():
        payload["content"] = content
    if thread_name:
        payload["thread_name"] = thread_name[:100]
    headers = {"Accept": "*/*"}
    if image_path is None or not image_path.is_file():
        return requests.post(url, headers=headers, json=payload, timeout=timeout)

    with image_path.open("rb") as fh:
        files = {"files[0]": (image_path.name, fh, "image/png")}
        return requests.post(
            url,
            headers=headers,
            data={"payload_json": json.dumps(payload)},
            files=files,
            timeout=timeout,
        )


def _send_summary_for_period(
    *,
    storage: StorageService,
    cfg: DailySummaryConfig,
    period: SummaryPeriod,
    log: callable,
) -> None:
    webhook_url = resolve_daily_summary_webhook_url(cfg_url=cfg.webhook_url)
    if not webhook_url:
        return

    root_dir = storage.db_path.parent.parent
    exports_dir = root_dir / "storage" / "exports"

    data = _query_summary(storage, period)
    chart_paths = _render_chart_images(
        data=data,
        exports_dir=exports_dir,
        top_n=max(3, int(cfg.top_items_limit)),
    )

    embed = _build_embed(data=data)
    ts = int(_utc_now().timestamp())
    content = f"**24h recap** — <t:{ts}:F>"

    starter = _discord_webhook_post_message(
        webhook_url=webhook_url,
        content=content,
        thread_name=_forum_thread_name(period=period),
        wait=True,
        timeout=30.0,
    )
    starter.raise_for_status()
    thread_id = _thread_id_from_webhook_message_response(starter)
    if thread_id is None:
        raise RuntimeError(
            "Daily recap starter posted but Discord did not return a forum thread id; "
            "ensure the webhook targets a forum or media channel."
        )

    for path in chart_paths:
        chart_resp = _discord_webhook_post_message(
            webhook_url=webhook_url,
            content=_chart_message_content(path),
            image_path=path,
            thread_id=thread_id,
            timeout=60.0,
        )
        chart_resp.raise_for_status()

    resp = _discord_webhook_post_message(
        webhook_url=webhook_url,
        embed=embed,
        thread_id=thread_id,
        timeout=30.0,
    )
    resp.raise_for_status()

    for path in chart_paths:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    log(
        "cycle",
        (
            f"Posted daily recap to Discord for {period.label} "
            f"(sales={data['total_sales']}, volume={_fmt_mirrors(data['total_volume'])}, "
            f"charts={len(chart_paths)}, forum_thread={bool(thread_id)})."
        ),
    )


def maybe_send_daily_summary_to_discord(
    *,
    storage: StorageService,
    cfg: DailySummaryConfig,
    log: callable,
) -> None:
    """
    Post a daily recap once per local calendar day after the configured schedule.

    Covers the **last 24 hours** ending at send time. State is stored in SQLite `daily_summary`.
    """
    if not cfg.enabled:
        return
    if not resolve_daily_summary_webhook_url(cfg_url=cfg.webhook_url):
        return

    tz = _tz_from_offset_minutes(cfg.tz_offset_minutes)
    now_utc = _utc_now()
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()
    after_schedule = (now_local.hour, now_local.minute) >= (
        int(cfg.schedule_hour),
        int(cfg.schedule_minute),
    )
    if not after_schedule:
        return

    state = storage.get_config(key="daily_summary") or {}
    last_sent_local_date = str(state.get("last_sent_local_date", "")).strip()
    if last_sent_local_date == today_local.isoformat():
        return

    period = _period_last_24_hours(end_utc=now_utc)
    try:
        _send_summary_for_period(storage=storage, cfg=cfg, period=period, log=log)
    except Exception as exc:  # noqa: BLE001
        log("warn", f"Daily recap Discord post failed: {exc}")
        return

    storage.set_config(
        key="daily_summary",
        value={
            "last_sent_at_utc": now_utc.isoformat(),
            "last_sent_local_date": today_local.isoformat(),
            "last_summary_period": period.label,
            "tz_offset_minutes": int(cfg.tz_offset_minutes),
            "schedule_hour": int(cfg.schedule_hour),
            "schedule_minute": int(cfg.schedule_minute),
        },
    )


def send_daily_summary_to_discord_now(
    *,
    storage: StorageService,
    cfg: DailySummaryConfig,
    period: SummaryPeriod | None = None,
) -> dict[str, Any]:
    """Manual send (e.g. admin); defaults to the last 24 hours ending now."""
    webhook_url = resolve_daily_summary_webhook_url(cfg_url=cfg.webhook_url)
    if not webhook_url:
        return {
            "ok": False,
            "error": (
                "Daily recap webhook is not configured. "
                "Set DISCORD_WEBHOOK_URL_DAILY_SUMMARY or DISCORD_WEBHOOK_URL in .env "
                "(or export it in the shell before running)."
            ),
        }

    if period is None:
        period = _period_last_24_hours()

    try:
        _send_summary_for_period(
            storage=storage,
            cfg=cfg,
            period=period,
            log=lambda _level, _msg: None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    now_utc = _utc_now()
    storage.set_config(
        key="daily_summary",
        value={
            "last_sent_at_utc": now_utc.isoformat(),
            "last_sent_local_date": now_utc.astimezone(_tz_from_offset_minutes(cfg.tz_offset_minutes)).date().isoformat(),
            "last_summary_period": period.label,
            "manual": True,
        },
    )
    return {"ok": True, "period": period.label}
