"""
Weekly retrain entry point (launched by the poller, see poller/ml_retrain.py).

Evaluates the profit-per-day estimator and the learned model on recorded history and writes
ML/models/profit_model.{pkl,json}. Exits 0 whenever the run completes, including when the model
stays disabled because it did not beat the estimator; non-zero only on errors.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ML.pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
