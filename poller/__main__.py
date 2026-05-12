"""Run the poller via ``python -m poller`` from the repository root."""

from __future__ import annotations

from env_loader import load_local_env

from .poll_item_prices import main


load_local_env()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
