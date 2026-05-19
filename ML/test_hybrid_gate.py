from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from server.data_service import load_config, save_config
from server.recommendation_service import recommend_investments


def _result_sources(payload: dict) -> list[str]:
    recs = payload.get("recommendations") or []
    return sorted({str(r.get("rankingSource") or "") for r in recs})


def _print_step(name: str, ok: bool, details: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"{name}: {status} {details}")


def main() -> None:
    original = load_config()
    try:
        r0 = recommend_investments(
            {
                "wealth": 100,
                "currency": "mirror",
                "risk": "balanced",
                "mode": "ranked",
                "limit": 8,
            }
        )
        src0 = _result_sources(r0)
        _print_step(
            "Step1 default heuristic only",
            (r0.get("mlShadow", {}).get("hybridEnabled") is False) and (src0 == ["heuristic"]),
            f"mlShadow={r0.get('mlShadow')} sources={src0}",
        )

        cfg = dict(original)
        cfg.update(
            {
                "ml_shadow_enabled": True,
                "ml_hybrid_enabled": True,
                "ml_hybrid_alpha_heuristic": 0.85,
                "ml_hybrid_min_confidence_tier": "medium",
            }
        )
        save_config(cfg)

        r1 = recommend_investments(
            {
                "wealth": 100,
                "currency": "mirror",
                "risk": "balanced",
                "mode": "ranked",
                "limit": 8,
            }
        )
        src1 = _result_sources(r1)
        applied1 = int((r1.get("mlTelemetry") or {}).get("hybridAppliedCandidates") or 0)
        _print_step(
            "Step2 conservative hybrid",
            r1.get("mlShadow", {}).get("hybridEnabled") is True,
            f"applied={applied1} sources={src1}",
        )

        cfg2 = dict(cfg)
        cfg2.update(
            {
                "ml_hybrid_alpha_heuristic": 0.7,
                "ml_hybrid_min_confidence_tier": "sparse",
            }
        )
        save_config(cfg2)

        r2 = recommend_investments(
            {
                "wealth": 100,
                "currency": "mirror",
                "risk": "balanced",
                "mode": "ranked",
                "limit": 8,
            }
        )
        applied2 = int((r2.get("mlTelemetry") or {}).get("hybridAppliedCandidates") or 0)
        _print_step(
            "Step3 aggressive increases use",
            applied2 >= applied1,
            f"conservative={applied1} aggressive={applied2}",
        )

    finally:
        save_config(original)

    r3 = recommend_investments(
        {
            "wealth": 100,
            "currency": "mirror",
            "risk": "balanced",
            "mode": "ranked",
            "limit": 8,
        }
    )
    src3 = _result_sources(r3)
    _print_step(
        "Step4 rollback heuristic",
        (r3.get("mlShadow", {}).get("hybridEnabled") is False) and (src3 == ["heuristic"]),
        f"mlShadow={r3.get('mlShadow')} sources={src3}",
    )
    print("Done")


if __name__ == "__main__":
    main()
