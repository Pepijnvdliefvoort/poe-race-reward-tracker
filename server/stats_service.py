from __future__ import annotations

import os
import platform
import socket
import time
from datetime import datetime, timezone
from typing import Any

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _to_mb(value_bytes: float | int | None) -> float | None:
    if value_bytes is None:
        return None
    try:
        return float(value_bytes) / (1024 * 1024)
    except Exception:
        return None


def system_stats_payload() -> dict[str, Any]:
    """
    Cross-platform system + process stats for lightweight dashboards.
    Requires psutil; if unavailable, returns ok=False with a helpful error.
    """
    if psutil is None:
        return {
            "ok": False,
            "error": "psutil is not installed. Run: pip install -r requirements.txt",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        }

    boot_time_s = None
    try:
        boot_time_s = float(psutil.boot_time())
    except Exception:
        boot_time_s = None

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()

    # psutil needs an interval sample; interval=None often returns 0.0 if not previously primed.
    try:
        cpu_percent = float(psutil.cpu_percent(interval=0.2))
    except Exception:
        cpu_percent = None

    try:
        cpu_count_logical = int(psutil.cpu_count(logical=True) or 0) or None
    except Exception:
        cpu_count_logical = None

    try:
        cpu_count_physical = int(psutil.cpu_count(logical=False) or 0) or None
    except Exception:
        cpu_count_physical = None

    try:
        load_1, load_5, load_15 = os.getloadavg()
        load = {"1m": float(load_1), "5m": float(load_5), "15m": float(load_15)}
    except Exception:
        load = None

    try:
        net = psutil.net_io_counters()
        net_io = {
            "rxMb": _to_mb(net.bytes_recv),
            "txMb": _to_mb(net.bytes_sent),
        }
    except Exception:
        net_io = None

    proc = psutil.Process()
    try:
        proc_mem = proc.memory_info()
        proc_rss_mb = _to_mb(proc_mem.rss)
        proc_vms_mb = _to_mb(proc_mem.vms)
    except Exception:
        proc_rss_mb = None
        proc_vms_mb = None

    try:
        proc_cpu_percent = float(proc.cpu_percent(interval=0.0))
    except Exception:
        proc_cpu_percent = None

    try:
        proc_create_time_s = float(proc.create_time())
    except Exception:
        proc_create_time_s = None

    try:
        proc_threads = int(proc.num_threads())
    except Exception:
        proc_threads = None

    return {
        "ok": True,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "generatedAtMs": _epoch_ms(),
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.system(),
        },
        "system": {
            "cpu": {
                "percent": cpu_percent,
                "loadAvg": load,
            },
            "memory": {
                "totalMb": _to_mb(vm.total),
                "usedMb": _to_mb(vm.used),
                "availableMb": _to_mb(vm.available),
                "usedPercent": float(vm.percent),
            },
            "swap": {
                "totalMb": _to_mb(sm.total),
                "usedMb": _to_mb(sm.used),
                "freeMb": _to_mb(sm.free),
                "usedPercent": float(sm.percent),
            },
            "bootTimeMs": int(boot_time_s * 1000) if boot_time_s is not None else None,
            "net": net_io,
        },
        "process": {
            "pid": int(proc.pid),
            "cpuPercent": proc_cpu_percent,
            "rssMb": proc_rss_mb,
            "createTimeMs": int(proc_create_time_s * 1000) if proc_create_time_s is not None else None,
            "uptimeMs": (int(time.time() * 1000) - int(proc_create_time_s * 1000))
            if proc_create_time_s is not None
            else None,
            "threads": proc_threads,
        },
    }

