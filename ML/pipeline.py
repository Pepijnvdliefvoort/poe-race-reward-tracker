"""
Weekly retrain: walk-forward evaluation of the estimator, the learned model and a random
baseline on recorded history, then train the final model and enable it only if it beat the
estimator by a clear margin on the weeks where it was actually used.

Run: python scripts/retrain_ml_pipeline.py   (or python -m ML.pipeline)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ML import model as model_mod
from ML.estimator import EstimatorParams, params_from_config
from ML.features import snapshots_at
from ML.market import Market, load_market
from ML.simulate import StrategyResult, decision_times, estimator_scorer, random_scorer, run_backtest

ROOT_DIR = Path(__file__).resolve().parents[1]

TOP_K = 5
# Gate for switching the ranking to the learned model. Deliberately strict: on synthetic data with
# no learnable signal, a 4-week / 10%-lift gate still enabled the model in 3 of 12 runs.
MIN_ACTIVE_FOLDS = 8
MIN_WEEKS_WON_SHARE = 0.75  # model must win at least this share of the evaluated weeks
REQUIRED_RELATIVE_LIFT = 0.10  # ...and beat the estimator's overall return/day by 10%


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None


def _read_market_config(con: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = con.execute("SELECT value_json FROM app_config WHERE key = 'market'").fetchone()
    except sqlite3.Error:
        return {}
    if not row or not row[0]:
        return {}
    try:
        cfg = json.loads(row[0])
    except ValueError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _restricted(result: StrategyResult, times: set[float]) -> StrategyResult:
    out = StrategyResult(result.name)
    out.trades = [t for t in result.trades if t.entry_ts in times]
    out.per_decision = {ts: v for ts, v in result.per_decision.items() if ts in times}
    return out


def evaluate_and_train(market: Market, params: EstimatorParams, *, top_k: int = TOP_K) -> tuple[Any, dict[str, Any]]:
    """Returns (final_model_or_None, metadata)."""
    times = decision_times(market, horizon_days=params.horizon_days)
    snaps_by_time = {ts: snapshots_at(market, ts) for ts in times}
    rows = model_mod.label_rows(market, snaps_by_time, params)

    wf = model_mod.WalkForwardScorer(rows, params)
    results = run_backtest(
        market,
        {"estimator": estimator_scorer(params), "model": wf, "random": random_scorer()},
        params=params,
        times=times,
        top_k=top_k,
        snapshots_by_time=snaps_by_time,
    )
    active = set(wf.active_times)
    est_active = _restricted(results["estimator"], active).summary()
    model_active = _restricted(results["model"], active).summary()

    est_rpd = est_active.get("returnPerDay")
    model_rpd = model_active.get("returnPerDay")
    folds_won = sum(
        1
        for ts in active
        if ts in results["model"].per_decision
        and results["model"].per_decision[ts] > results["estimator"].per_decision.get(ts, float("-inf"))
    )

    reason: str | None = None
    if len(active) < MIN_ACTIVE_FOLDS:
        reason = f"not-enough-history: model evaluated on {len(active)} weeks, needs {MIN_ACTIVE_FOLDS}"
    elif model_rpd is None or est_rpd is None:
        reason = "no-trades-to-compare"
    elif model_rpd <= 0:
        reason = "model-return-not-positive"
    elif model_rpd < est_rpd + abs(est_rpd) * REQUIRED_RELATIVE_LIFT:
        reason = "model-did-not-beat-estimator"
    elif folds_won < MIN_WEEKS_WON_SHARE * len(active):
        reason = f"model-inconsistent: won {folds_won} of {len(active)} weeks"
    enabled = reason is None

    final_model = None
    if len(rows) >= model_mod.MIN_TRAIN_ROWS:
        final_model = model_mod.fit(rows)
    elif enabled:
        enabled, reason = False, "not-enough-training-rows"

    meta: dict[str, Any] = {
        "trainedAtUtc": model_mod.utc_now_iso(),
        "enabled": enabled,
        "disabledReason": reason,
        "sklearnVersion": model_mod.sklearn_version(),
        "features": model_mod.MODEL_FEATURES,
        "params": asdict(params),
        "topK": top_k,
        "data": {
            "startUtc": _iso(market.start_ts),
            "endUtc": _iso(market.end_ts),
            "variants": len(market.variants),
            "decisionWeeks": len(times),
            "labeledRows": len(rows),
            "modelActiveWeeks": len(active),
        },
        "gate": {
            "minActiveWeeks": MIN_ACTIVE_FOLDS,
            "minWeeksWonShare": MIN_WEEKS_WON_SHARE,
            "requiredRelativeLift": REQUIRED_RELATIVE_LIFT,
            "modelWeeksWon": folds_won,
        },
        "backtest": {
            "allWeeks": {name: res.summary() for name, res in results.items()},
            "modelActiveWeeks": {"estimator": est_active, "model": model_active},
        },
    }
    return final_model, meta


def run(db_path: Path, root_dir: Path, *, params: EstimatorParams | None = None, top_k: int = TOP_K) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    try:
        market = load_market(con)
        if params is None:
            params = params_from_config(_read_market_config(con))
    finally:
        con.close()

    final_model, meta = evaluate_and_train(market, params, top_k=top_k)
    if final_model is not None:
        model_mod.save(root_dir, final_model, meta)
    else:
        model_mod.save_meta_only(root_dir, meta)
    return meta


def _print_summary(meta: dict[str, Any]) -> None:
    d = meta["data"]
    print(f"Data {d['startUtc']} -> {d['endUtc']}: {d['variants']} variants, {d['decisionWeeks']} decision weeks, {d['labeledRows']} labeled rows")
    for scope, block in (("all weeks", meta["backtest"]["allWeeks"]), ("model-active weeks", meta["backtest"]["modelActiveWeeks"])):
        print(f"Backtest ({scope}):")
        for name, s in block.items():
            if not s.get("trades"):
                print(f"  {name:10} no trades")
                continue
            print(
                f"  {name:10} return/day={s['returnPerDay'] * 100:+.3f}%  per trade={s['meanReturnPerTrade'] * 100:+.1f}%  "
                f"sold={s['soldRate']:.0%}  days held={s['meanDaysHeld']:.1f}  trades={s['trades']}"
            )
    print(f"Model enabled: {meta['enabled']}" + (f" ({meta['disabledReason']})" if meta["disabledReason"] else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate and retrain the profit-per-day ranking model.")
    parser.add_argument("--db", default=str(ROOT_DIR / "data" / "market.db"))
    parser.add_argument("--root", default=str(ROOT_DIR), help="Repo root (models are written to <root>/ML/models)")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"DB not found: {db_path}")
        return 2
    meta = run(db_path, Path(args.root), top_k=max(1, args.top_k))
    _print_summary(meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
