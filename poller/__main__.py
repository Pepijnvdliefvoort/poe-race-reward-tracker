"""Run the poller via ``python -m poller`` from the repository root."""

from __future__ import annotations

from .poll_item_prices import main


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
