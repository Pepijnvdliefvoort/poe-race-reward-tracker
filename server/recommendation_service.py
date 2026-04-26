from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.storage_service import ServerStorage

ROOT_DIR = Path(__file__).resolve().parents[1]

VALID_CURRENCIES = {"mirror", "divine"}
VALID_RISKS = {"safe", "balanced", "speculative"}
VALID_MODES = {"ranked", "portfolio"}
STALE_PRICE_DAYS = 90
RECENT_WINDOW_DAYS = 30
MAX_RECOMMENDATIONS = 8


class RecommendationInputError(ValueError):
    """Raised when a companion request cannot be safely evaluated."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _latest_divines_per_mirror(storage: ServerStorage) -> float | None:
    con = storage.connect()
    try:
        row = con.execute(
            """
            SELECT divines_per_mirror
            FROM poll_runs
            WHERE divines_per_mirror IS NOT NULL AND divines_per_mirror > 0
            ORDER BY started_at_utc DESC
            LIMIT 1
            """
        ).fetchone()
        return _finite_positive(row["divines_per_mirror"] if row else None)
    finally:
        con.close()


def _load_variant_history(storage: ServerStorage) -> dict[int, list[dict[str, Any]]]:
    con = storage.connect()
    try:
        rows = con.execute(
            """
            SELECT
              v.id AS variant_id,
              i.name AS base_item_name,
              v.display_name,
              v.mode,
              v.sort_order,
              i.icon_path,
              pr.league,
              pr.cycle_number,
              pr.started_at_utc,
              ip.requested_at_utc,
              ip.query_id,
              ip.total_results,
              ip.used_results,
              ip.lowest_mirror,
              ip.median_mirror,
              ip.highest_mirror,
              ip.inf_confirmed_transfer,
              ip.inf_likely_instant_sale,
              ip.inf_likely_non_instant_online,
              ip.inf_relist_same_seller,
              ip.inf_reprice_same_seller
            FROM item_polls ip
            JOIN poll_runs pr ON pr.id = ip.poll_run_id
            JOIN item_variants v ON v.id = ip.item_variant_id
            JOIN items i ON i.id = v.item_id
            ORDER BY v.sort_order ASC, v.display_name ASC, ip.requested_at_utc ASC
            """
        ).fetchall()
    finally:
        con.close()

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["variant_id"])].append(dict(row))
    return grouped


def _row_price(row: dict[str, Any]) -> float | None:
    for key in ("lowest_mirror", "median_mirror", "highest_mirror"):
        value = _finite_positive(row.get(key))
        if value is not None:
            return value
    return None


def _current_price(rows: list[dict[str, Any]], now: datetime) -> tuple[float | None, bool, float | None]:
    latest = rows[-1] if rows else {}
    live_low = _finite_positive(latest.get("lowest_mirror"))
    if live_low is not None:
        latest_dt = _parse_iso(latest.get("requested_at_utc"))
        age_days = (now - latest_dt).total_seconds() / 86400 if latest_dt else None
        return live_low, False, age_days

    for row in reversed(rows):
        price = _row_price(row)
        if price is None:
            continue
        price_dt = _parse_iso(row.get("requested_at_utc"))
        age_days = (now - price_dt).total_seconds() / 86400 if price_dt else None
        if age_days is None or age_days <= STALE_PRICE_DAYS:
            return price, True, age_days
    return None, False, None


def _trend_pct(rows: list[dict[str, Any]], current_price: float, now: datetime) -> float | None:
    cutoff = now.timestamp() - RECENT_WINDOW_DAYS * 86400
    baseline: float | None = None
    for row in rows:
        row_dt = _parse_iso(row.get("requested_at_utc"))
        if not row_dt or row_dt.timestamp() < cutoff:
            continue
        price = _row_price(row)
        if price is not None:
            baseline = price
            break
    if baseline is None or baseline <= 0:
        return None
    return ((current_price - baseline) / baseline) * 100.0


def _recent_sales(rows: list[dict[str, Any]], now: datetime) -> int:
    cutoff = now.timestamp() - RECENT_WINDOW_DAYS * 86400
    total = 0
    for row in rows:
        row_dt = _parse_iso(row.get("requested_at_utc"))
        if not row_dt or row_dt.timestamp() < cutoff:
            continue
        total += int(row.get("inf_confirmed_transfer") or 0)
        total += int(row.get("inf_likely_instant_sale") or 0)
        total += int(row.get("inf_likely_non_instant_online") or 0)
    return max(0, total)


def _fit_score(price: float, wealth_mirror: float, risk: str) -> tuple[float, float]:
    ratio = price / wealth_mirror
    ideal = {"safe": 0.18, "balanced": 0.32, "speculative": 0.55}[risk]
    width = {"safe": 0.24, "balanced": 0.38, "speculative": 0.55}[risk]
    score = 1.0 - abs(ratio - ideal) / width
    if ratio > 0.92:
        score -= 0.35
    if ratio < 0.03:
        score -= 0.15
    return _clamp(score), ratio


def _trend_score(trend: float | None, risk: str) -> float:
    if trend is None:
        return 0.45
    if risk == "safe":
        return _clamp(1.0 - abs(trend) / 35.0)
    if risk == "balanced":
        if -18.0 <= trend <= 22.0:
            return 0.82
        if trend < -18.0:
            return _clamp(0.64 + min(abs(trend), 45.0) / 180.0)
        return _clamp(0.7 - (trend - 22.0) / 90.0)
    if trend < 0:
        return _clamp(0.58 + min(abs(trend), 60.0) / 75.0)
    return _clamp(0.48 + min(trend, 55.0) / 90.0)


def _category(score: float, risk: str, sales_30d: int, trend: float | None, total_results: int) -> str:
    if risk == "speculative" or (trend is not None and abs(trend) >= 25) or total_results < 5:
        return "Speculative"
    if sales_30d >= 5 and total_results >= 8:
        return "Liquid"
    if score >= 72:
        return "Best fit"
    if trend is not None and trend < -10:
        return "Value watch"
    return "Watchlist"


def _reasons(
    *,
    ratio: float,
    trend: float | None,
    sales_30d: int,
    total_results: int,
    used_results: int,
    price_is_last_known: bool,
) -> list[str]:
    reasons: list[str] = []
    reasons.append(f"Uses {ratio * 100:.0f}% of your available wealth.")
    if sales_30d > 0:
        reasons.append(f"About {sales_30d} inferred sale signals in the last {RECENT_WINDOW_DAYS} days.")
    elif total_results >= 10:
        reasons.append("Healthy listing depth, but recent sale signals are quiet.")
    else:
        reasons.append("Thin market, so price movement may be noisy.")

    if trend is not None:
        if trend <= -10:
            reasons.append(f"Price is down {abs(trend):.0f}% over the recent window.")
        elif trend >= 10:
            reasons.append(f"Price is up {trend:.0f}% over the recent window.")
        else:
            reasons.append("Recent price trend is relatively stable.")
    if used_results > 0:
        reasons.append(f"Latest poll used {used_results} listings for pricing.")
    if price_is_last_known:
        reasons.append("Current floor was missing, so this uses the last known price.")
    return reasons[:4]


def _warnings(*, age_days: float | None, total_results: int, price_is_last_known: bool, ratio: float) -> list[str]:
    warnings: list[str] = []
    if price_is_last_known:
        warnings.append("Price is carried forward from an earlier poll.")
    if age_days is not None and age_days > 1:
        warnings.append(f"Latest usable price is {age_days:.0f} days old.")
    if total_results < 5:
        warnings.append("Low listing count can make this market easy to move.")
    if ratio > 0.75:
        warnings.append("This would concentrate most of your wealth in one item.")
    return warnings


def _portfolio_targets(risk: str) -> dict[str, float]:
    return {
        "safe": {"deploy": 0.60, "position": 0.22, "min_score": 48},
        "balanced": {"deploy": 0.75, "position": 0.30, "min_score": 42},
        "speculative": {"deploy": 0.85, "position": 0.40, "min_score": 35},
    }[risk]


def _build_portfolio_plan(
    *,
    recommendations: list[dict[str, Any]],
    wealth_mirror: float,
    risk: str,
) -> dict[str, Any]:
    targets = _portfolio_targets(risk)
    deploy_target = wealth_mirror * targets["deploy"]
    max_position = wealth_mirror * targets["position"]
    min_score = int(targets["min_score"])

    positions: list[dict[str, Any]] = []
    deployed = 0.0
    used_bases: set[str] = set()

    for rec in recommendations:
        if deployed >= deploy_target:
            break
        if int(rec.get("score") or 0) < min_score:
            continue

        base_key = str(rec.get("baseItemName") or rec.get("itemName") or "").strip().lower()
        if base_key and base_key in used_bases:
            continue

        price = _finite_positive(rec.get("priceMirror"))
        if price is None:
            continue

        remaining_target = max(0.0, deploy_target - deployed)
        position_cap = min(max_position, remaining_target)
        units = int(position_cap // price)
        if units <= 0 and price <= remaining_target and price <= max_position:
            units = 1
        if units <= 0:
            continue

        allocation = round(units * price, 2)
        if allocation <= 0:
            continue

        item = dict(rec)
        item["portfolioUnits"] = units
        item["portfolioAllocationMirror"] = allocation
        item["portfolioShare"] = round(allocation / wealth_mirror, 3)
        item["portfolioReason"] = (
            f"Caps this position near {targets['position'] * 100:.0f}% of wealth while contributing to a "
            f"{targets['deploy'] * 100:.0f}% deployment target."
        )
        positions.append(item)
        deployed += allocation
        if base_key:
            used_bases.add(base_key)

    deployed = round(deployed, 2)
    cash = round(max(0.0, wealth_mirror - deployed), 2)
    target = round(deploy_target, 2)
    notes = [
        f"Targets about {targets['deploy'] * 100:.0f}% deployed for a {risk} profile.",
        f"Keeps about {max(0.0, 1.0 - targets['deploy']) * 100:.0f}% liquid for stale data, repricing, or better entries.",
        f"Caps each position near {targets['position'] * 100:.0f}% of wealth to reduce concentration.",
    ]
    if deployed < target * 0.75:
        notes.append("Could not deploy the full target without forcing low-score, stale, or oversized positions.")

    return {
        "targetDeployedMirror": target,
        "deployedMirror": deployed,
        "cashReserveMirror": cash,
        "deploymentPct": round(deployed / wealth_mirror, 3) if wealth_mirror > 0 else 0,
        "positions": positions,
        "notes": notes,
    }


def recommend_investments(request: dict[str, Any], *, root_dir: Path | None = None) -> dict[str, Any]:
    wealth = _finite_positive(request.get("wealth"))
    if wealth is None:
        raise RecommendationInputError("wealth must be a positive number")

    currency = str(request.get("currency") or "mirror").strip().lower()
    if currency not in VALID_CURRENCIES:
        raise RecommendationInputError("currency must be mirror or divine")

    risk = str(request.get("risk") or "balanced").strip().lower()
    if risk not in VALID_RISKS:
        raise RecommendationInputError("risk must be safe, balanced, or speculative")

    mode = str(request.get("mode") or "ranked").strip().lower()
    if mode not in VALID_MODES:
        raise RecommendationInputError("mode must be ranked or portfolio")

    limit_raw = request.get("limit", MAX_RECOMMENDATIONS)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = MAX_RECOMMENDATIONS
    limit = max(1, min(MAX_RECOMMENDATIONS, limit))

    storage = ServerStorage(root_dir or ROOT_DIR)
    divines_per_mirror = _latest_divines_per_mirror(storage)
    if currency == "divine":
        if divines_per_mirror is None:
            raise RecommendationInputError("cannot convert divine wealth without a recent divine per mirror rate")
        wealth_mirror = wealth / divines_per_mirror
    else:
        wealth_mirror = wealth

    if wealth_mirror <= 0:
        raise RecommendationInputError("wealth converts to zero mirrors")

    now = _utc_now()
    histories = _load_variant_history(storage)
    weights = {
        "safe": {"fit": 0.35, "liquidity": 0.35, "supply": 0.20, "trend": 0.10},
        "balanced": {"fit": 0.30, "liquidity": 0.25, "supply": 0.15, "trend": 0.30},
        "speculative": {"fit": 0.25, "liquidity": 0.15, "supply": 0.10, "trend": 0.50},
    }[risk]

    recommendations: list[dict[str, Any]] = []
    skipped = {"unaffordable": 0, "no_price": 0, "stale": 0}
    for rows in histories.values():
        if not rows:
            continue
        latest = rows[-1]
        price, price_is_last_known, age_days = _current_price(rows, now)
        if price is None:
            skipped["no_price"] += 1
            continue
        if age_days is not None and age_days > STALE_PRICE_DAYS:
            skipped["stale"] += 1
            continue
        if price > wealth_mirror * 0.98:
            skipped["unaffordable"] += 1
            continue

        total_results = int(latest.get("total_results") or 0)
        used_results = int(latest.get("used_results") or 0)
        sales_30d = _recent_sales(rows, now)
        trend = _trend_pct(rows, price, now)
        fit_score, ratio = _fit_score(price, wealth_mirror, risk)
        liquidity_score = _clamp((sales_30d / 8.0) * 0.75 + (total_results / 24.0) * 0.25)
        supply_score = _clamp(total_results / 18.0)
        trend_score = _trend_score(trend, risk)
        score = (
            fit_score * weights["fit"]
            + liquidity_score * weights["liquidity"]
            + supply_score * weights["supply"]
            + trend_score * weights["trend"]
        )
        score_100 = round(score * 100)

        target_allocation = {"safe": 0.35, "balanced": 0.55, "speculative": 0.75}[risk] * wealth_mirror
        units = max(1, int(target_allocation // price))
        max_units = max(1, int((wealth_mirror * 0.95) // price))
        units = min(units, max_units)
        allocation_mirror = round(units * price, 2)

        recommendations.append(
            {
                "itemName": str(latest.get("display_name") or latest.get("base_item_name") or ""),
                "baseItemName": str(latest.get("base_item_name") or ""),
                "mode": str(latest.get("mode") or ""),
                "imagePath": latest.get("icon_path"),
                "queryId": str(latest.get("query_id") or ""),
                "league": str(latest.get("league") or "Standard"),
                "priceMirror": round(price, 2),
                "priceIsLastKnown": price_is_last_known,
                "wealthShare": round(ratio, 3),
                "suggestedUnits": units,
                "suggestedAllocationMirror": allocation_mirror,
                "score": score_100,
                "category": _category(score_100, risk, sales_30d, trend, total_results),
                "trendPct30d": round(trend, 1) if trend is not None else None,
                "inferredSales30d": sales_30d,
                "totalListings": total_results,
                "usedListings": used_results,
                "latestPollAt": str(latest.get("requested_at_utc") or ""),
                "reasons": _reasons(
                    ratio=ratio,
                    trend=trend,
                    sales_30d=sales_30d,
                    total_results=total_results,
                    used_results=used_results,
                    price_is_last_known=price_is_last_known,
                ),
                "warnings": _warnings(
                    age_days=age_days,
                    total_results=total_results,
                    price_is_last_known=price_is_last_known,
                    ratio=ratio,
                ),
            }
        )

    recommendations.sort(
        key=lambda rec: (
            -int(rec["score"]),
            -int(rec["inferredSales30d"]),
            float(rec["priceMirror"]),
            str(rec["itemName"]),
        )
    )

    portfolio = _build_portfolio_plan(recommendations=recommendations, wealth_mirror=wealth_mirror, risk=risk)

    return {
        "ok": True,
        "generatedAt": now.isoformat(),
        "wealth": wealth,
        "currency": currency,
        "wealthMirror": round(wealth_mirror, 2),
        "divinesPerMirror": divines_per_mirror,
        "risk": risk,
        "mode": mode,
        "recommendations": recommendations[:limit],
        "portfolio": portfolio if mode == "portfolio" else None,
        "skipped": skipped,
        "disclaimer": "These are market estimates from inferred listing data, not guaranteed returns.",
    }
