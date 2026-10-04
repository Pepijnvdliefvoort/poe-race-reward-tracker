"""Deferred poller restart after a deploy.

deploy/deploy_on_vps.sh writes a flag file instead of restarting the poller mid-cycle. The poller
consumes it between cycles and exits cleanly; systemd (Restart=always) starts it on the new code.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RESTART_FLAG_PATH = "/var/lib/poe-market-flips/poller-restart-requested"
RESTART_FLAG_ENV = "POE_POLLER_RESTART_FLAG"


def restart_flag_path() -> Path:
    raw = (os.getenv(RESTART_FLAG_ENV) or "").strip()
    return Path(raw or DEFAULT_RESTART_FLAG_PATH)


def consume_restart_request(path: Path | None = None) -> str | None:
    """Return the flag's contents (deployed commit, may be empty) and delete it; None if absent."""
    flag = path or restart_flag_path()
    try:
        content = flag.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None
    except OSError:
        content = ""
    try:
        flag.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Can't clear it: don't report a request, or the poller would exit after every cycle.
        return None
    return content


def peek_restart_request(path: Path | None = None) -> dict[str, str] | None:
    """Describe a pending restart without consuming it (for the admin panel); None if absent."""
    flag = path or restart_flag_path()
    try:
        stat = flag.stat()
        content = flag.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    requested_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {"commit": content, "requested_at_utc": requested_at}
