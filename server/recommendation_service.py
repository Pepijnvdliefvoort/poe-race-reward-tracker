"""
Companion recommendations: rank affordable items by expected % return per day held.

The ranking comes from ML.estimator (a transparent formula over recent sales, listing floors and
sale rates). When the weekly retrain has enabled the learned model (it beat the estimator in the
walk-forward trading simulation), the model's predicted return/day is used for ordering instead.
The same ML.features / ML.estimator code runs in the backtest, so what is shown here is what was
evaluated.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ML import model as model_mod
from ML.estimator import Estimate, EstimatorParams, estimate, params_from_config
from ML.features import Snapshot, market_sale_rate, snapshot
from ML.market import Market, load_market
from server.data_service import _get_image_path
from server.storage_service import ServerStorage

ROOT_DIR = Path(__file__).resolve().parents[1]

VALID_CURRENCIES = {"mirror", "divine"}
VALID_RISKS = {"safe", "balanced", "speculative"}
VALID_MODES = {"ranked", "portfolio"}
MAX_RECOMMENDATIONS = 8
MIN_FLIP_PROFIT_MIRRORS = 1.0
MARKET_CACHE_TTL_SECONDS = 120.0

# Per risk profile: minimum sell chance within the horizon and allowed confidence tiers.
RISK_FILTERS: dict[str, dict[str, Any]] = {
    "safe": {"min_sell_probability": 0.5, "confidence": {"medium", "strong"}},
    "balanced": {"min_sell_probability": 0.25, "confidence": {"sparse", "medium", "strong"}},
    "speculative": {"min_sell_probability": 0.0, "confidence": {"sparse", "medium", "strong"}},
}

_market_cache_lock = threading.Lock()
_market_cache: dict[str, Any] = {"key": None, "loaded_at": 0.0, "market": None}


class RecommendationInputError(ValueError):
    """Raised when a companion request cannot be safely evaluated."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _pct(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value * 100.0, digits)


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


def _load_market_cached(storage: ServerStorage) -> Market:
    """Full history load is the expensive part; reuse it for a couple of minutes."""
    key = str(storage.db_path)
    now = time.monotonic()
    with _market_cache_lock:
        if _market_cache["key"] == key and now - _market_cache["loaded_at"] < MARKET_CACHE_TTL_SECONDS:
            return _market_cache["market"]
    con = storage.connect()
    try:
        market = load_market(con)
    finally:
        con.close()
    with _market_cache_lock:
        _market_cache.update({"key": key, "loaded_at": now, "market": market})
    return market


def _load_latest_poll_rows(storage: ServerStorage) -> dict[int, dict[str, Any]]:
    """Latest poll per variant with display fields (name, image, query id, league)."""
    con = storage.connect()
    try:
        rows = con.execute(
            """
            SELECT
              v.id AS variant_id, i.name AS base_item_name, v.display_name, v.mode, v.image_name_filter,
              v.sort_order, i.icon_path, ip.id AS item_poll_id, ip.query_id, ip.requested_at_utc, pr.league
            FROM item_polls ip
            JOIN (SELECT item_variant_id, MAX(id) AS max_id FROM item_polls GROUP BY item_variant_id) latest
              ON latest.max_id = ip.id
            JOIN poll_runs pr ON pr.id = ip.poll_run_id
            JOIN item_variants v ON v.id = ip.item_variant_id
            JOIN items i ON i.id = v.item_id
            """
        ).fetchall()
    finally:
        con.close()
    return {int(r["variant_id"]): dict(r) for r in rows}


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


def _whole_mirror_relist_price(next_market_price: float) -> float | None:
    if next_market_price <= 1:
        return None
    candidate = math.floor(next_market_price - 1e-6)
    if candidate <= 0 or candidate >= next_market_price:
        return None
    return float(candidate)


def _flip_opportunity(ladder_prices: list[float]) -> dict[str, Any]:
    """Immediate ladder gap: buy the only floor listing, relist just under the next one."""
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


def _max_units(snap: Snapshot, est: Estimate, params: EstimatorParams) -> int:
    """
    Copies worth buying at once: no more than are listed, and no more than the market is expected
    to absorb in half the horizon at our ask (each extra copy waits behind the previous one).
    """
    absorbable = int(est.sale_rate_per_day * params.horizon_days / 2.0)
    return max(1, min(snap.total_listings or 1, absorbable))


def _category(est: Estimate) -> str:
    if est.confidence == "sparse":
        return "Speculative"
    if est.expected_days <= 14:
        return "Quick flip"
    if est.expected_days <= 45:
        return "Steady"
    return "Slow hold"


def _reasons(snap: Snapshot, est: Estimate, params: EstimatorParams) -> list[str]:
    reasons: list[str] = []
    if snap.sale_anchor is not None:
        reasons.append(
            f"Recent sales put fair value near {snap.fair_value:.2f} mirrors "
            f"({snap.sales_90d} sale{'s' if snap.sales_90d != 1 else ''} in 90 days)."
        )
    else:
        reasons.append(f"No recent sales; fair value falls back to listing floors ({snap.fair_value:.2f} mirrors).")
    reasons.append(
        f"Buy at {snap.entry_price:.2f}, list at {est.ask_price:.2f}: {est.return_if_sold * 100:+.1f}% if it sells."
    )
    reasons.append(
        f"About {est.expected_days:.0f} days to sell at that price "
        f"({est.sell_probability * 100:.0f}% chance within {params.horizon_days:.0f} days)."
    )
    if snap.listings_change_30d is not None and abs(snap.listings_change_30d) >= 0.2:
        direction = "down" if snap.listings_change_30d < 0 else "up"
        reasons.append(f"Listings are {direction} {abs(snap.listings_change_30d) * 100:.0f}% versus a month ago.")
    return reasons


def _warnings(snap: Snapshot, est: Estimate, wealth_share: float) -> list[str]:
    warnings: list[str] = []
    if est.confidence == "sparse":
        warnings.append("Very few recent sales, so the sell-time estimate leans on market-wide averages.")
    if snap.entry_age_days > 1:
        warnings.append(f"Latest buyable price is {snap.entry_age_days:.0f} days old.")
    if snap.entry_price > snap.fair_value:
        warnings.append("The cheapest listing is above recent sale-based value.")
    if wealth_share > 0.75:
        warnings.append("This would concentrate most of your wealth in one item.")
    return warnings


def _portfolio_targets(risk: str) -> dict[str, float]:
    return {
        "safe": {"deploy": 0.60, "position": 0.22},
        "balanced": {"deploy": 0.75, "position": 0.30},
        "speculative": {"deploy": 0.85, "position": 0.40},
    }[risk]


def _build_portfolio_plan(*, recommendations: list[dict[str, Any]], wealth_mirror: float, risk: str) -> dict[str, Any]:
    targets = _portfolio_targets(risk)
    deploy_target = wealth_mirror * targets["deploy"]
    max_position = wealth_mirror * targets["position"]

    positions: list[dict[str, Any]] = []
    deployed = 0.0
    used_bases: set[str] = set()

    for rec in recommendations:
        if deployed >= deploy_target:
            break
        base_key = str(rec.get("baseItemName") or rec.get("itemName") or "").strip().lower()
        if base_key and base_key in used_bases:
            continue
        price = _finite_positive(rec.get("priceMirror"))
        if price is None:
            continue

        remaining_target = max(0.0, deploy_target - deployed)
        position_cap = min(max_position, remaining_target)
        units = min(int(position_cap // price), int(rec.get("maxUnits") or 1))
        if units <= 0 and price <= remaining_target and price <= max_position:
            units = 1
        if units <= 0:
            continue

        allocation = round(units * price, 2)
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
    target = round(deploy_target, 2)
    notes = [
        f"Targets about {targets['deploy'] * 100:.0f}% deployed for a {risk} profile.",
        f"Caps each position near {targets['position'] * 100:.0f}% of wealth to reduce concentration.",
    ]
    if deployed < target * 0.75:
        notes.append("Could not deploy the full target without positions that have no positive expected return.")
    return {
        "targetDeployedMirror": target,
        "deployedMirror": deployed,
        "cashReserveMirror": round(max(0.0, wealth_mirror - deployed), 2),
        "deploymentPct": round(deployed / wealth_mirror, 3) if wealth_mirror > 0 else 0,
        "positions": positions,
        "notes": notes,
    }


def _recommendation_image_path(row: dict[str, Any]) -> str | None:
    mode = str(row.get("mode") or "").strip()
    is_aa = True if mode == "aa" else False if mode == "normal" else None
    resolved = _get_image_path(
        str(row.get("base_item_name") or "").strip(), is_aa, str(row.get("image_name_filter") or "").strip() or None
    )
    if resolved:
        return resolved
    raw = str(row.get("icon_path") or "").strip()
    return raw or None


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

    try:
        limit = int(request.get("limit", MAX_RECOMMENDATIONS))
    except (TypeError, ValueError):
        limit = MAX_RECOMMENDATIONS
    limit = max(1, min(MAX_RECOMMENDATIONS, limit))

    base_dir = Path(root_dir) if root_dir is not None else ROOT_DIR
    storage = ServerStorage(base_dir)
    divines_per_mirror = _latest_divines_per_mirror(storage)
    if currency == "divine":
        if divines_per_mirror is None:
            raise RecommendationInputError("cannot convert divine wealth without a recent divine per mirror rate")
        wealth_mirror = wealth / divines_per_mirror
    else:
        wealth_mirror = wealth
    if wealth_mirror <= 0:
        raise RecommendationInputError("wealth converts to zero mirrors")

    try:
        params = params_from_config(storage.get_market_config())
    except (sqlite3.Error, ValueError):
        params = EstimatorParams()
    model, model_meta, model_reason = model_mod.load_for_serving(base_dir)

    now = _utc_now()
    now_ts = now.timestamp()
    market = _load_market_cached(storage)
    latest_rows = _load_latest_poll_rows(storage)
    ladders = _load_latest_listing_ladders(storage, [int(r["item_poll_id"]) for r in latest_rows.values()])
    rate = market_sale_rate(market, now_ts)
    filters = RISK_FILTERS[risk]

    skipped = {"unaffordable": 0, "no_price": 0, "not_profitable": 0, "risk_filtered": 0}
    candidates: list[tuple[Snapshot, Estimate]] = []
    for vid, hist in market.variants.items():
        if vid not in latest_rows:
            continue
        snap = snapshot(hist, now_ts, market_rate=rate)
        if snap is None:
            skipped["no_price"] += 1
            continue
        if snap.entry_price > wealth_mirror * 0.98:
            skipped["unaffordable"] += 1
            continue
        est = estimate(snap, params)
        if est.return_per_day <= 0:
            skipped["not_profitable"] += 1
            continue
        if est.sell_probability < filters["min_sell_probability"] or est.confidence not in filters["confidence"]:
            skipped["risk_filtered"] += 1
            continue
        candidates.append((snap, est))

    model_scores: dict[int, float] = {}
    if model is not None and candidates:
        try:
            model_scores = model_mod.predict(model, [s for s, _ in candidates], params)
        except Exception as exc:  # noqa: BLE001
            model_scores, model_reason = {}, f"model-inference-failed: {exc}"
    use_model = bool(model_scores)

    def rank_key(item: tuple[Snapshot, Estimate]) -> tuple[float, float, str]:
        snap, est = item
        primary = model_scores.get(snap.variant_id, est.return_per_day) if use_model else est.return_per_day
        return (-primary, -est.sell_probability, str(latest_rows[snap.variant_id].get("display_name") or ""))

    candidates.sort(key=rank_key)
    n = len(candidates)
    target_share = {"safe": 0.35, "balanced": 0.55, "speculative": 0.75}[risk]

    recommendations: list[dict[str, Any]] = []
    for rank, (snap, est) in enumerate(candidates):
        row = latest_rows[snap.variant_id]
        wealth_share = snap.entry_price / wealth_mirror
        max_units = _max_units(snap, est, params)
        units = max(1, min(int((target_share * wealth_mirror) // snap.entry_price), max_units))
        recommendations.append(
            {
                "itemName": str(row.get("display_name") or row.get("base_item_name") or ""),
                "baseItemName": str(row.get("base_item_name") or ""),
                "mode": str(row.get("mode") or ""),
                "imagePath": _recommendation_image_path(row),
                "queryId": str(row.get("query_id") or ""),
                "league": str(row.get("league") or "Standard"),
                "priceMirror": round(snap.entry_price, 2),
                "wealthShare": round(wealth_share, 3),
                "suggestedUnits": units,
                "maxUnits": max_units,
                "suggestedAllocationMirror": round(units * snap.entry_price, 2),
                # Percentile of this item among today's candidates (100 = best).
                "score": round(100 * (n - rank) / n),
                "rankingSource": "model" if use_model else "estimator",
                "category": _category(est),
                "confidence": est.confidence,
                "estimate": {
                    "askPriceMirror": round(est.ask_price, 2),
                    "fairValueMirror": round(snap.fair_value, 2),
                    "saleAnchorMirror": round(snap.sale_anchor, 2) if snap.sale_anchor is not None else None,
                    "returnIfSoldPct": _pct(est.return_if_sold, 1),
                    "expectedReturnPct": _pct(est.expected_return, 1),
                    "expectedDays": round(est.expected_days, 1),
                    "returnPerDayPct": _pct(est.return_per_day, 3),
                    "sellProbability": round(est.sell_probability, 3),
                    "horizonDays": params.horizon_days,
                    "sales90d": snap.sales_90d,
                    "salesAtOrAboveAsk90d": est.sales_at_or_above_ask_90d,
                },
                "modelReturnPerDayPct": _pct(model_scores.get(snap.variant_id), 3) if use_model else None,
                "trendPct30d": _pct(snap.floor_momentum, 1),
                "inferredSales30d": snap.sales_30d,
                "totalListings": snap.total_listings,
                "latestPollAt": str(row.get("requested_at_utc") or ""),
                "flip": _flip_opportunity(ladders.get(snap.variant_id, [])),
                "reasons": _reasons(snap, est, params),
                "warnings": _warnings(snap, est, wealth_share),
            }
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
        "ranking": {
            "method": "model" if use_model else "estimator",
            "metric": "expected % return per day held",
            "modelEnabled": use_model,
            "modelReason": None if use_model else model_reason,
            "modelTrainedAt": (model_meta or {}).get("trainedAtUtc"),
            "horizonDays": params.horizon_days,
            "feePct": params.fee_pct,
            "undercutPct": params.undercut_pct,
            "marketSaleRatePerDay": round(rate, 5),
        },
        "recommendations": recommendations[:limit],
        "portfolio": portfolio if mode == "portfolio" else None,
        "skipped": skipped,
        "disclaimer": "These are market estimates from inferred sales and listings, not guaranteed returns.",
    }
