"""
Companion track record: log the picks the companion shows and check later whether they would have
sold at the suggested ask.

Each pick is stored once per (item, ISO week, plan, ask), mirroring the backtest's weekly decisions.
Outcomes are replayed on recorded history with the same rules as the backtest (ML.simulate.realize_trade:
the listings ahead must clear first, and only the plan's own buyers count). A pick is final once it
sold, or once its horizon has passed; final outcomes are stored so they survive the history window.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from ML.estimator import Estimate, EstimatorParams
from ML.features import DAY, Snapshot
from ML.market import Market
from ML.simulate import realize_trade

RECENT_LIMIT = 25


def _iso_week(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def log_picks(
    con: sqlite3.Connection,
    picks: list[tuple[Snapshot, Estimate]],
    params: EstimatorParams,
    *,
    ranking_source: str,
    now: datetime,
) -> None:
    """Store shown picks (in rank order). A pick already logged this week keeps its best rank."""
    week = _iso_week(now)
    for rank, (snap, est) in enumerate(picks, start=1):
        con.execute(
            """
            INSERT INTO companion_picks(
              item_variant_id, pick_week, created_at_utc, plan, entry_price_mirror, ask_price_mirror,
              ask_whole_mirrors, queue_ahead, sell_probability, expected_days, expected_return, return_per_day,
              horizon_days, fee_pct, best_rank, ranking_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_variant_id, pick_week, plan, ask_price_mirror)
            DO UPDATE SET best_rank = MIN(best_rank, excluded.best_rank)
            """,
            (
                int(snap.variant_id),
                week,
                now.isoformat(),
                str(est.plan),
                float(snap.entry_price),
                round(float(est.ask_price), 6),
                int(est.ask_whole_mirrors or 0),
                int(est.queue_ahead),
                float(est.sell_probability),
                float(est.expected_days),
                float(est.expected_return),
                float(est.return_per_day),
                float(params.horizon_days),
                float(params.fee_pct),
                int(rank),
                str(ranking_source),
            ),
        )
    con.commit()


def _replay(market: Market, row: sqlite3.Row) -> tuple[str, Any]:
    """('final', outcome), ('pending', None) or ('unknown', None) for one logged pick."""
    hist = market.variants.get(int(row["item_variant_id"]))
    created = datetime.fromisoformat(str(row["created_at_utc"]))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    ts = created.timestamp()
    if hist is None or market.end_ts is None or market.start_ts is None or ts < market.start_ts:
        return "unknown", None
    horizon = float(row["horizon_days"])
    observed = (market.end_ts - ts) / DAY
    if observed <= 0:
        return "pending", None
    snap = SimpleNamespace(ts=ts, entry_price=float(row["entry_price_mirror"]), variant_id=int(row["item_variant_id"]))
    outcome = realize_trade(
        hist,
        snap,
        ask=float(row["ask_price_mirror"]),
        horizon_days=min(horizon, observed),
        fee_pct=float(row["fee_pct"]),
        queue=int(row["queue_ahead"]),
        plan=str(row["plan"]),
    )
    if outcome.sold or observed >= horizon:
        return "final", outcome
    return "pending", None


def evaluate_and_summarize(con: sqlite3.Connection, market: Market, *, now: datetime) -> dict[str, Any]:
    """Resolve picks that became final, then summarize predicted vs actual."""
    for row in con.execute(
        "SELECT * FROM companion_picks WHERE outcome_evaluated_at_utc IS NULL ORDER BY id"
    ).fetchall():
        state, outcome = _replay(market, row)
        if state == "final":
            con.execute(
                """UPDATE companion_picks SET outcome_sold = ?, outcome_days = ?, outcome_return = ?,
                   outcome_evaluated_at_utc = ? WHERE id = ?""",
                (int(outcome.sold), float(outcome.days), float(outcome.ret), now.isoformat(), int(row["id"])),
            )
    con.commit()

    rows = con.execute(
        """
        SELECT p.*, v.display_name FROM companion_picks p JOIN item_variants v ON v.id = p.item_variant_id
        ORDER BY p.created_at_utc DESC, p.best_rank ASC
        """
    ).fetchall()
    final = [r for r in rows if r["outcome_evaluated_at_utc"] is not None]
    top = [r for r in final if int(r["best_rank"]) <= 5]

    def block(rs: list[sqlite3.Row]) -> dict[str, Any]:
        if not rs:
            return {"picks": 0}
        days = sum(float(r["outcome_days"]) for r in rs)
        return {
            "picks": len(rs),
            "predictedSellRate": round(sum(float(r["sell_probability"]) for r in rs) / len(rs), 3),
            "actualSellRate": round(sum(int(r["outcome_sold"]) for r in rs) / len(rs), 3),
            "predictedReturnPct": round(100 * sum(float(r["expected_return"]) for r in rs) / len(rs), 1),
            "actualReturnPct": round(100 * sum(float(r["outcome_return"]) for r in rs) / len(rs), 1),
            "actualReturnPerDayPct": round(100 * sum(float(r["outcome_return"]) for r in rs) / days, 3) if days else None,
        }

    def pick_dict(r: sqlite3.Row) -> dict[str, Any]:
        final_row = r["outcome_evaluated_at_utc"] is not None
        return {
            "itemName": str(r["display_name"] or ""),
            "week": str(r["pick_week"]),
            "createdAt": str(r["created_at_utc"]),
            "bestRank": int(r["best_rank"]),
            "plan": str(r["plan"]),
            "entryPriceMirror": round(float(r["entry_price_mirror"]), 3),
            "askPriceMirror": round(float(r["ask_price_mirror"]), 3),
            "askWholeMirrors": int(r["ask_whole_mirrors"]) or None,
            "sellProbability": round(float(r["sell_probability"]), 3),
            "expectedDays": round(float(r["expected_days"]), 1),
            "status": ("sold" if int(r["outcome_sold"]) else "unsold") if final_row else "pending",
            "daysToSell": round(float(r["outcome_days"]), 1) if final_row and int(r["outcome_sold"]) else None,
            "returnPct": round(100 * float(r["outcome_return"]), 1) if final_row else None,
        }

    return {
        "ok": True,
        "logged": len(rows),
        "pending": len(rows) - len(final),
        "evaluated": block(final),
        "top5": block(top),
        "recent": [pick_dict(r) for r in rows[:RECENT_LIMIT]],
    }
