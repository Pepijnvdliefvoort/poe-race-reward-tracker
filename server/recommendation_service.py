from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.data_service import _get_image_path
from server.storage_service import ServerStorage

ROOT_DIR = Path(__file__).resolve().parents[1]

VALID_CURRENCIES = {"mirror", "divine"}
VALID_RISKS = {"safe", "balanced", "speculative"}
VALID_MODES = {"ranked", "portfolio"}
STALE_PRICE_DAYS = 90
RECENT_WINDOW_DAYS = 30
MAX_RECOMMENDATIONS = 8
MIN_FLIP_PROFIT_MIRRORS = 1.0


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
              ip.id AS item_poll_id,
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


def _load_latest_listing_ladders(storage: ServerStorage, item_poll_ids: list[int]) -> dict[int, list[float]]:
    ids = sorted({int(v) for v in item_poll_ids if int(v) > 0})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    con = storage.connect()
    try:
        rows = con.execute(
            f"""
            SELECT ip.item_variant_id, ls.amount, ls.currency, ls.is_instant_buyout
            FROM listing_snapshots ls
            JOIN item_polls ip ON ip.id = ls.item_poll_id
            WHERE ls.item_poll_id IN ({placeholders})
            ORDER BY ip.item_variant_id ASC, ls.rank ASC
            """,
            ids,
        ).fetchall()
    finally:
        con.close()

    ladders: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        if int(row["is_instant_buyout"] or 0) != 1:
            continue
        currency = str(row["currency"] or "").strip().lower()
        if currency not in {"mirror", "mirrors", "mirror of kalandra"}:
            continue
        amount = _finite_positive(row["amount"])
        if amount is None:
            continue
        # These markets normally trade in whole mirrors; ignore fractional/other-currency rows
        # for flip-profit simulation so we do not invent an impossible relist price.
        if abs(amount - round(amount)) > 1e-6:
            continue
        ladders[int(row["item_variant_id"])].append(float(round(amount)))
    return dict(ladders)


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
        return 0.28
    if risk == "safe":
        # Stable is useful, but it should not outrank actual demand or a clean ladder gap.
        return _clamp(0.62 - abs(trend) / 70.0)
    if risk == "balanced":
        if -8.0 <= trend <= 8.0:
            return 0.42
        if 8.0 < trend <= 25.0:
            return _clamp(0.5 + trend / 70.0)
        if trend < -18.0:
            return _clamp(0.42 + min(abs(trend), 45.0) / 180.0)
        return _clamp(0.62 - (trend - 25.0) / 100.0)
    if trend < 0:
        return _clamp(0.36 + min(abs(trend), 60.0) / 95.0)
    return _clamp(0.38 + min(trend, 55.0) / 75.0)


def _demand_score(sales_30d: int, trend: float | None) -> float:
    sales_score = _clamp(sales_30d / 5.0)
    trend_bonus = 0.0 if trend is None else _clamp(trend / 40.0, 0.0, 0.35)
    return _clamp(sales_score + trend_bonus)


def _market_penalty(*, sales_30d: int, trend: float | None, flip: dict[str, Any], ladder_prices: list[float]) -> float:
    penalty = 0.0
    flat_or_unknown = trend is None or abs(trend) < 1.0
    if sales_30d == 0 and flat_or_unknown and not flip.get("viable"):
        penalty += 0.34
    floor_stock = int(flip.get("floorStock") or 0)
    if floor_stock > 1:
        penalty += min(0.22, 0.07 * (floor_stock - 1))
    if len(ladder_prices) < 2:
        penalty += 0.08
    return penalty


def _category(score: float, risk: str, sales_30d: int, trend: float | None, flip_viable: bool) -> str:
    flat_or_unknown = trend is None or abs(trend) < 1.0
    if flip_viable:
        return "Best fit" if score >= 72 else "Value watch"
    if sales_30d == 0 and flat_or_unknown:
        return "Watchlist"
    if risk == "speculative" or (trend is not None and abs(trend) >= 25):
        return "Speculative"
    if sales_30d >= 5:
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
    else:
        reasons.append("No inferred sale signals in the recent window, so demand is unproven.")

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
    if total_results == 0:
        warnings.append("No live listings were reported in the latest poll.")
    if ratio > 0.75:
        warnings.append("This would concentrate most of your wealth in one item.")
    return warnings


def _whole_mirror_relist_price(next_market_price: float) -> float | None:
    if next_market_price <= 1:
        return None
    candidate = math.floor(next_market_price - 1e-6)
    if candidate <= 0 or candidate >= next_market_price:
        return None
    return float(candidate)


def _flip_opportunity(ladder_prices: list[float]) -> dict[str, Any]:
    prices = sorted(p for p in ladder_prices if p > 0)
    if len(prices) < 2:
        return {
            "viable": False,
            "floorStock": len(prices),
            "reason": "Not enough instant whole-mirror listings to estimate a resale gap.",
        }

    buy_price = prices[0]
    floor_stock = sum(1 for p in prices if abs(p - buy_price) <= 1e-6)
    next_after_one = prices[1]

    if floor_stock > 1:
        return {
            "viable": False,
            "buyPriceMirror": buy_price,
            "floorStock": floor_stock,
            "nextMarketPriceMirror": next_after_one,
            "reason": f"There are {floor_stock} instant listings at {buy_price:g} mirror, so buying one does not move the floor.",
        }

    relist_price = _whole_mirror_relist_price(next_after_one)
    if relist_price is None or relist_price <= buy_price:
        return {
            "viable": False,
            "buyPriceMirror": buy_price,
            "floorStock": floor_stock,
            "nextMarketPriceMirror": next_after_one,
            "reason": "The next listing is too close to create a profitable whole-mirror undercut.",
        }

    profit = relist_price - buy_price
    if profit + 1e-6 < MIN_FLIP_PROFIT_MIRRORS:
        return {
            "viable": False,
            "buyPriceMirror": buy_price,
            "floorStock": floor_stock,
            "nextMarketPriceMirror": next_after_one,
            "relistPriceMirror": relist_price,
            "expectedProfitMirror": round(profit, 2),
            "reason": "The gross flip gap is below the 1 mirror minimum profit target.",
        }

    return {
        "viable": True,
        "buyPriceMirror": buy_price,
        "floorStock": floor_stock,
        "nextMarketPriceMirror": next_after_one,
        "relistPriceMirror": relist_price,
        "expectedProfitMirror": round(profit, 2),
        "expectedProfitPct": round((profit / buy_price) * 100.0, 1) if buy_price > 0 else None,
        "sellCondition": f"Relist immediately for {relist_price:g} mirrors, below the next listing at {next_after_one:g}.",
        "reason": "Buying the floor listing creates a whole-mirror resale gap.",
    }


def _hold_30d_estimate(
    *,
    price: float,
    trend: float | None,
    sales_30d: int,
    total_results: int,
    risk: str,
) -> dict[str, Any]:
    trend_component = 0.0 if trend is None else _clamp(trend / 100.0, -0.35, 0.35)
    risk_multiplier = {"safe": 0.45, "balanced": 0.65, "speculative": 0.9}[risk]
    liquidity_bonus = min(sales_30d, 8) * 0.006
    supply_penalty = 0.04 if total_results >= 20 else 0.02 if total_results >= 12 else 0.0
    expected_return = _clamp((trend_component * risk_multiplier) + liquidity_bonus - supply_penalty, -0.25, 0.35)
    model_price = max(0.0, price * (1.0 + expected_return))
    expected_sell_price = float(max(1, math.floor(model_price))) if model_price > 0 else 0.0
    whole_mirror_profit = expected_sell_price - price

    if whole_mirror_profit > 0:
        sell_timing = "Hold up to 30 days, then list into strength if demand and sales remain active."
    elif whole_mirror_profit < 0:
        sell_timing = "Avoid a 30-day hold unless the ladder tightens or league-merge demand starts to show."
    else:
        sell_timing = "Treat this as flat over 30 days; profit depends more on a good entry than passive appreciation."

    if sales_30d >= 5 and expected_return >= 0:
        cycle_note = "Recent sale signals suggest healthier demand, similar to periods with more Standard activity."
    elif sales_30d <= 1:
        cycle_note = "Demand looks quiet; if this is far from a league merge, prices can drift lower while activity is low."
    else:
        cycle_note = "This does not know the exact league-merge date, so it uses recent trend and sales as the activity proxy."

    return {
        "horizonDays": 30,
        "expectedPriceMirror": round(expected_sell_price, 2),
        "modelPriceMirror": round(model_price, 2),
        "expectedProfitMirror": round(whole_mirror_profit, 2),
        "expectedReturnPct": round((whole_mirror_profit / price) * 100.0, 1) if price > 0 else None,
        "sellTiming": sell_timing,
        "cycleNote": f"{cycle_note} The sell estimate is rounded down to a whole-mirror listing price.",
    }


def _mode_to_is_aa(mode: Any) -> bool | None:
    if mode == "aa":
        return True
    if mode == "normal":
        return False
    return None


def _recommendation_image_path(row: dict[str, Any]) -> str | None:
    base_name = str(row.get("base_item_name") or "").strip()
    mode = str(row.get("mode") or "").strip()
    resolved = _get_image_path(base_name, _mode_to_is_aa(mode))
    if resolved:
        return resolved
    raw = str(row.get("icon_path") or "").strip()
    return raw or None


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
    latest_poll_ids = [int(rows[-1]["item_poll_id"]) for rows in histories.values() if rows and rows[-1].get("item_poll_id")]
    listing_ladders = _load_latest_listing_ladders(storage, latest_poll_ids)
    weights = {
        "safe": {"fit": 0.30, "demand": 0.35, "trend": 0.20, "flip": 0.15},
        "balanced": {"fit": 0.25, "demand": 0.30, "trend": 0.25, "flip": 0.20},
        "speculative": {"fit": 0.20, "demand": 0.20, "trend": 0.35, "flip": 0.25},
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

        total_results = int(latest.get("total_results") or 0)
        used_results = int(latest.get("used_results") or 0)
        sales_30d = _recent_sales(rows, now)
        trend = _trend_pct(rows, price, now)
        variant_id = int(latest.get("variant_id") or 0)
        ladder_prices = listing_ladders.get(variant_id, [])
        ladder_floor = min(ladder_prices) if ladder_prices else None
        entry_price = ladder_floor if ladder_floor is not None else price
        if entry_price > wealth_mirror * 0.98:
            skipped["unaffordable"] += 1
            continue

        flip = _flip_opportunity(ladder_prices)
        hold_30d = _hold_30d_estimate(
            price=entry_price,
            trend=trend,
            sales_30d=sales_30d,
            total_results=total_results,
            risk=risk,
        )
        fit_score, ratio = _fit_score(entry_price, wealth_mirror, risk)
        demand_score = _demand_score(sales_30d, trend)
        trend_score = _trend_score(trend, risk)
        flip_score = 1.0 if flip.get("viable") else 0.0
        market_penalty = _market_penalty(sales_30d=sales_30d, trend=trend, flip=flip, ladder_prices=ladder_prices)
        score = (
            fit_score * weights["fit"]
            + demand_score * weights["demand"]
            + trend_score * weights["trend"]
            + flip_score * weights["flip"]
            - market_penalty
        )
        score = _clamp(score)
        score_100 = round(score * 100)

        target_allocation = {"safe": 0.35, "balanced": 0.55, "speculative": 0.75}[risk] * wealth_mirror
        units = max(1, int(target_allocation // entry_price))
        max_units = max(1, int((wealth_mirror * 0.95) // entry_price))
        units = min(units, max_units)
        allocation_mirror = round(units * entry_price, 2)
        reasons = _reasons(
            ratio=ratio,
            trend=trend,
            sales_30d=sales_30d,
            total_results=total_results,
            used_results=used_results,
            price_is_last_known=price_is_last_known,
        )
        if ladder_floor is not None:
            reasons.append("Entry price uses the latest instant whole-mirror listing ladder.")

        recommendations.append(
            {
                "itemName": str(latest.get("display_name") or latest.get("base_item_name") or ""),
                "baseItemName": str(latest.get("base_item_name") or ""),
                "mode": str(latest.get("mode") or ""),
                "imagePath": _recommendation_image_path(latest),
                "queryId": str(latest.get("query_id") or ""),
                "league": str(latest.get("league") or "Standard"),
                "priceMirror": round(entry_price, 2),
                "pricingSource": "instant whole-mirror ladder" if ladder_floor is not None else "latest price history",
                "priceIsLastKnown": price_is_last_known,
                "wealthShare": round(ratio, 3),
                "suggestedUnits": units,
                "suggestedAllocationMirror": allocation_mirror,
                "score": score_100,
                "category": _category(score_100, risk, sales_30d, trend, bool(flip.get("viable"))),
                "trendPct30d": round(trend, 1) if trend is not None else None,
                "inferredSales30d": sales_30d,
                "totalListings": total_results,
                "usedListings": used_results,
                "latestPollAt": str(latest.get("requested_at_utc") or ""),
                "flip": flip,
                "hold30d": hold_30d,
                "reasons": reasons[:5],
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
