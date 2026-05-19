from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
ML_DIR = ROOT_DIR / "ML"
LIVE_DATASET_CSV = ML_DIR / "training_30d.csv"
LIVE_DATASET_META = ML_DIR / "training_30d.meta.json"
LIVE_CLASSIFIER = ML_DIR / "model_sellprob_30d.pkl"
LIVE_REGRESSOR = ML_DIR / "model_execprice_30d.pkl"
LIVE_TRAINING_REPORT = ML_DIR / "training_30d.training_report.json"
LIVE_BACKTEST_REPORT = ML_DIR / "training_30d.backtest_report.json"


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


def _run(cmd: list[str], cwd: Path) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _get_nested(payload: dict[str, Any], keys: list[str]) -> Any:
    cur: Any = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _metric_gate(
    *,
    name: str,
    baseline: float | None,
    candidate: float | None,
    max_drop: float,
    higher_is_better: bool,
) -> GateResult:
    if candidate is None:
        return GateResult(name=name, passed=False, detail="candidate metric is missing")
    if baseline is None:
        return GateResult(name=name, passed=True, detail=f"no baseline; candidate={candidate:.6f}")

    if higher_is_better:
        delta = candidate - baseline
        passed = delta >= -max_drop
        return GateResult(
            name=name,
            passed=passed,
            detail=(
                f"baseline={baseline:.6f}, candidate={candidate:.6f}, "
                f"delta={delta:.6f}, max_drop={max_drop:.6f}"
            ),
        )

    delta = candidate - baseline
    passed = delta <= max_drop
    return GateResult(
        name=name,
        passed=passed,
        detail=(
            f"baseline={baseline:.6f}, candidate={candidate:.6f}, "
            f"increase={delta:.6f}, max_increase={max_drop:.6f}"
        ),
    )


def _lift_gate(
    *,
    k: int,
    baseline_lift: float | None,
    candidate_lift: float | None,
    max_drop: float,
) -> list[GateResult]:
    gates: list[GateResult] = []
    name_prefix = f"lift@{k}"

    if candidate_lift is None:
        gates.append(GateResult(name=f"{name_prefix}-present", passed=False, detail="candidate lift missing"))
        return gates

    gates.append(
        GateResult(
            name=f"{name_prefix}-nonnegative",
            passed=candidate_lift >= 0.0,
            detail=f"candidate_lift={candidate_lift:.6f} (must be >= 0)",
        )
    )

    if baseline_lift is not None:
        delta = candidate_lift - baseline_lift
        gates.append(
            GateResult(
                name=f"{name_prefix}-vs-baseline",
                passed=delta >= -max_drop,
                detail=(
                    f"baseline_lift={baseline_lift:.6f}, candidate_lift={candidate_lift:.6f}, "
                    f"delta={delta:.6f}, max_drop={max_drop:.6f}"
                ),
            )
        )
    else:
        gates.append(
            GateResult(
                name=f"{name_prefix}-vs-baseline",
                passed=True,
                detail=f"no baseline; candidate_lift={candidate_lift:.6f}",
            )
        )

    return gates


def _archive_live_artifacts(archive_dir: Path) -> list[str]:
    copied: list[str] = []
    archive_dir.mkdir(parents=True, exist_ok=True)

    for path in [
        LIVE_CLASSIFIER,
        LIVE_REGRESSOR,
        LIVE_TRAINING_REPORT,
        LIVE_BACKTEST_REPORT,
        LIVE_DATASET_CSV,
        LIVE_DATASET_META,
    ]:
        if path.exists():
            dest = archive_dir / path.name
            shutil.copy2(path, dest)
            copied.append(str(dest))

    return copied


def _prune_old_children(*, parent: Path, keep_days: int) -> list[str]:
    removed: list[str] = []
    if keep_days < 0 or not parent.exists():
        return removed

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(keep_days))
    for child in parent.iterdir():
        try:
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
        except Exception:
            continue
        if mtime >= cutoff:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed.append(str(child))
        except Exception:
            continue
    return removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/retrain_ml_pipeline.py",
        description="Rebuild dataset, train candidate models, backtest, and promote only if quality gates pass.",
    )
    parser.add_argument("--python", default=sys.executable, help="Python interpreter to use for subprocess steps")
    parser.add_argument("--db", default=str(ROOT_DIR / "data" / "market.db"), help="Path to SQLite market DB")
    parser.add_argument("--friction", type=float, default=0.0, help="Fixed friction for backtest in mirrors")
    parser.add_argument(
        "--required-lift-k",
        default="20,50,100",
        help="Comma-separated top-k values that must pass lift gates",
    )
    parser.add_argument("--max-pr-auc-drop", type=float, default=0.02, help="Max allowed PR-AUC drop vs baseline")
    parser.add_argument("--max-roc-auc-drop", type=float, default=0.02, help="Max allowed ROC-AUC drop vs baseline")
    parser.add_argument("--max-mape-increase", type=float, default=0.10, help="Max allowed MAPE increase vs baseline")
    parser.add_argument("--max-lift-drop", type=float, default=0.05, help="Max allowed lift drop vs baseline")
    parser.add_argument("--retain-candidate-days", type=int, default=90, help="Days to keep ML/candidates runs")
    parser.add_argument("--retain-archive-days", type=int, default=180, help="Days to keep ML/archive snapshots")
    parser.add_argument("--force-promote", action="store_true", help="Promote candidate even if gates fail")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate_dir = ML_DIR / "candidates" / ts
    archive_dir = ML_DIR / "archive" / ts
    candidate_dir.mkdir(parents=True, exist_ok=True)

    candidate_classifier = candidate_dir / "model_sellprob_30d.pkl"
    candidate_regressor = candidate_dir / "model_execprice_30d.pkl"
    candidate_training_report = candidate_dir / "training_30d.training_report.json"
    candidate_backtest_report = candidate_dir / "training_30d.backtest_report.json"

    _run(
        [
            args.python,
            "ML/build_training_dataset.py",
            "--db",
            str(Path(args.db)),
            "--out-csv",
            str(LIVE_DATASET_CSV),
            "--out-meta",
            str(LIVE_DATASET_META),
        ],
        cwd=ROOT_DIR,
    )

    _run(
        [
            args.python,
            "ML/train_model.py",
            "--csv",
            str(LIVE_DATASET_CSV),
            "--out-classifier",
            str(candidate_classifier),
            "--out-regressor",
            str(candidate_regressor),
            "--out-report",
            str(candidate_training_report),
        ],
        cwd=ROOT_DIR,
    )

    _run(
        [
            args.python,
            "ML/backtest_recommendations.py",
            "--csv",
            str(LIVE_DATASET_CSV),
            "--classifier",
            str(candidate_classifier),
            "--regressor",
            str(candidate_regressor),
            "--out-report",
            str(candidate_backtest_report),
            "--friction",
            str(float(args.friction)),
        ],
        cwd=ROOT_DIR,
    )

    baseline_training = _load_json(LIVE_TRAINING_REPORT) if LIVE_TRAINING_REPORT.exists() else None
    baseline_backtest = _load_json(LIVE_BACKTEST_REPORT) if LIVE_BACKTEST_REPORT.exists() else None

    cand_training = _load_json(candidate_training_report)
    cand_backtest = _load_json(candidate_backtest_report)

    gates: list[GateResult] = []

    baseline_pr_auc = _to_float(_get_nested(baseline_training or {}, ["models", "classifier", "test", "prAuc"]))
    baseline_roc_auc = _to_float(_get_nested(baseline_training or {}, ["models", "classifier", "test", "rocAuc"]))
    baseline_mape = _to_float(_get_nested(baseline_training or {}, ["models", "regressor", "test", "mape"]))

    cand_pr_auc = _to_float(_get_nested(cand_training, ["models", "classifier", "test", "prAuc"]))
    cand_roc_auc = _to_float(_get_nested(cand_training, ["models", "classifier", "test", "rocAuc"]))
    cand_mape = _to_float(_get_nested(cand_training, ["models", "regressor", "test", "mape"]))

    gates.append(
        _metric_gate(
            name="classifier-pr-auc",
            baseline=baseline_pr_auc,
            candidate=cand_pr_auc,
            max_drop=float(args.max_pr_auc_drop),
            higher_is_better=True,
        )
    )
    gates.append(
        _metric_gate(
            name="classifier-roc-auc",
            baseline=baseline_roc_auc,
            candidate=cand_roc_auc,
            max_drop=float(args.max_roc_auc_drop),
            higher_is_better=True,
        )
    )
    gates.append(
        _metric_gate(
            name="regressor-mape",
            baseline=baseline_mape,
            candidate=cand_mape,
            max_drop=float(args.max_mape_increase),
            higher_is_better=False,
        )
    )

    required_k = [int(v.strip()) for v in str(args.required_lift_k).split(",") if v.strip()]
    for k in required_k:
        key = str(k)
        baseline_lift = _to_float(
            _get_nested(baseline_backtest or {}, ["ranking", "lift", key, "avgRealizedEdgeAllLift"])
        )
        cand_lift = _to_float(_get_nested(cand_backtest, ["ranking", "lift", key, "avgRealizedEdgeAllLift"]))
        gates.extend(
            _lift_gate(
                k=k,
                baseline_lift=baseline_lift,
                candidate_lift=cand_lift,
                max_drop=float(args.max_lift_drop),
            )
        )

    gates_passed = all(g.passed for g in gates)
    should_promote = gates_passed or bool(args.force_promote)

    archived_paths: list[str] = []
    promoted_paths: list[str] = []
    if should_promote:
        archived_paths = _archive_live_artifacts(archive_dir)

        for src, dst in [
            (candidate_classifier, LIVE_CLASSIFIER),
            (candidate_regressor, LIVE_REGRESSOR),
            (candidate_training_report, LIVE_TRAINING_REPORT),
            (candidate_backtest_report, LIVE_BACKTEST_REPORT),
        ]:
            shutil.copy2(src, dst)
            promoted_paths.append(str(dst))

    summary = {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "candidateDir": str(candidate_dir),
        "archiveDir": str(archive_dir),
        "dbPath": str(Path(args.db)),
        "friction": float(args.friction),
        "requiredLiftK": required_k,
        "thresholds": {
            "maxPrAucDrop": float(args.max_pr_auc_drop),
            "maxRocAucDrop": float(args.max_roc_auc_drop),
            "maxMapeIncrease": float(args.max_mape_increase),
            "maxLiftDrop": float(args.max_lift_drop),
        },
        "retention": {
            "retainCandidateDays": int(args.retain_candidate_days),
            "retainArchiveDays": int(args.retain_archive_days),
        },
        "gatesPassed": gates_passed,
        "forcePromote": bool(args.force_promote),
        "promoted": should_promote,
        "gates": [
            {"name": g.name, "passed": g.passed, "detail": g.detail}
            for g in gates
        ],
        "archived": archived_paths,
        "promotedPaths": promoted_paths,
        "candidateArtifacts": {
            "classifier": str(candidate_classifier),
            "regressor": str(candidate_regressor),
            "trainingReport": str(candidate_training_report),
            "backtestReport": str(candidate_backtest_report),
        },
    }

    removed_candidates = _prune_old_children(parent=ML_DIR / "candidates", keep_days=int(args.retain_candidate_days))
    removed_archives = _prune_old_children(parent=ML_DIR / "archive", keep_days=int(args.retain_archive_days))
    summary["retentionCleanup"] = {
        "removedCandidates": removed_candidates,
        "removedArchives": removed_archives,
    }

    summary_path = candidate_dir / "promotion_summary.json"
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print(f"Candidate artifacts: {candidate_dir}")
    print(f"Promotion summary: {summary_path}")
    if should_promote:
        print("Promotion status: PROMOTED")
        return 0

    print("Promotion status: REJECTED (candidate kept for review)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
