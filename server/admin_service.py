from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_service import CSV_PATH

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
SERVER_LOG_PATH = LOG_DIR / "server.log"
POLLER_LOG_PATH = LOG_DIR / "poller.log"
POLLER_STDIO_LOG_PATH = LOG_DIR / "poller-stdio.log"
VISITORS_PATH = LOG_DIR / "visitors.jsonl"
IP_GEO_CACHE_PATH = LOG_DIR / "ip_geo_cache.json"

_visit_lock = threading.Lock()
_geo_lock = threading.Lock()
_geo_cache: dict[str, dict[str, Any]] | None = None
_poller_log_lock = threading.Lock()

_DEFAULT_SYSTEMD_POLLER_SERVICE = "poe-market-poller"


def _run_systemctl(args: list[str], timeout_s: float = 12.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "systemctl not found"
    except subprocess.TimeoutExpired:
        return 124, "", "systemctl timed out"


def _restart_poller_systemd(service: str) -> dict[str, Any]:
    rc, out, err = _run_systemctl(["restart", service])
    ok = rc == 0
    # Best-effort status snippet (non-fatal if it fails).
    rc2, out2, err2 = _run_systemctl(["is-active", service], timeout_s=6.0)
    active = out2.strip() if rc2 == 0 else ""
    return {
        "ok": ok,
        "action": "restarted",
        "mode": "systemd",
        "service": service,
        "systemctl": {"rc": rc, "stdout": out, "stderr": err},
        "active": active or None,
        "activeCheck": {"rc": rc2, "stdout": out2, "stderr": err2},
    }


def _append_poller_log(msg: str, *, level: str = "info", name: str = "poller") -> None:
    """
    Append a JSONL entry to poller.log so the admin poller console can show
    operator actions even across page reloads.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": (level or "info").lower(),
        "name": name,
        "msg": msg,
    }
    line = json.dumps(payload, ensure_ascii=False)
    with _poller_log_lock:
        with POLLER_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _stop_poller_systemd(service: str) -> dict[str, Any]:
    rc, out, err = _run_systemctl(["stop", service])
    ok = rc == 0
    rc2, out2, err2 = _run_systemctl(["is-active", service], timeout_s=6.0)
    active = out2.strip() if rc2 == 0 else ""
    return {
        "ok": ok,
        "action": "stopped",
        "mode": "systemd",
        "service": service,
        "systemctl": {"rc": rc, "stdout": out, "stderr": err},
        "active": active or None,
        "activeCheck": {"rc": rc2, "stdout": out2, "stderr": err2},
    }


def _kill_external_pollers() -> dict[str, Any]:
    """
    Kill pollers not owned by this server process.

    This is primarily for local dev: if you start `poll_item_prices.py` manually in a
    PowerShell window, hitting "restart" should not create a second poller.
    """
    killed: list[int] = []
    errors: list[str] = []

    if os.name == "nt":
        # Use PowerShell to find and kill *any* poller processes.
        #
        # Local dev often starts the poller as:
        #   python.exe poll_item_prices.py
        # which does not include the full repo path in the command line, so we
        # match on the script name only. This keeps "Restart poller" from ever
        # spawning a second concurrent poller.
        script = r"""
$procs = Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and ($_.CommandLine -match "poll_item_prices\.py")
}
foreach ($p in $procs) {
  try {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    Write-Output ("killed:" + $p.ProcessId)
  } catch {
    Write-Output ("error:" + $p.ProcessId + ":" + $_.Exception.Message)
  }
}
"""
        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=12.0,
                check=False,
            )
            lines = (p.stdout or "").splitlines()
            for line in lines:
                line = line.strip()
                if line.startswith("killed:"):
                    try:
                        killed.append(int(line.split(":", 1)[1]))
                    except ValueError:
                        continue
                elif line.startswith("error:"):
                    errors.append(line)
            stderr = (p.stderr or "").strip()
            if stderr:
                errors.append(stderr)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    else:
        # On Linux, systemd is the preferred mode. This is fallback-only.
        try:
            p = subprocess.run(
                ["pgrep", "-f", "poll_item_prices.py"],
                capture_output=True,
                text=True,
                timeout=6.0,
                check=False,
            )
            for raw in (p.stdout or "").split():
                try:
                    pid = int(raw.strip())
                except ValueError:
                    continue
                if pid == os.getpid():
                    continue
                try:
                    os.kill(pid, 15)
                    killed.append(pid)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"kill {pid}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    return {"killedPids": killed, "errors": errors}


class PollerManager:
    """
    Manage the poller as a subprocess owned by the dashboard server.

    This is optional: if POE_POLLER_AUTOSTART is not set, the poller will only be
    started when an admin triggers a restart/start endpoint.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._last_start_ts: float | None = None

    def _default_cmd(self) -> list[str]:
        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / "poll_item_prices.py"
        return [sys.executable, str(script)]

    def _build_cmd(self) -> list[str]:
        raw = (os.environ.get("POE_POLLER_CMD") or "").strip()
        if not raw:
            return self._default_cmd()
        # Very small "shell-like" split; users can always use a wrapper script if needed.
        return raw.split()

    def _ensure_started_unlocked(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        LOG_DIR.mkdir(parents=True, exist_ok=True)

        cmd = self._build_cmd()
        # Redirect stdout/stderr to a separate file so it doesn't disappear.
        # poller.log is written by structured logging inside the poller.
        stdio_fh = POLLER_STDIO_LOG_PATH.open("a", encoding="utf-8", buffering=1)
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=stdio_fh,
                stderr=stdio_fh,
                text=True,
            )
            self._last_start_ts = time.time()
            return self._proc
        except Exception:
            try:
                stdio_fh.close()
            except Exception:
                pass
            raise

    def start(self) -> dict[str, Any]:
        with self._lock:
            proc = self._ensure_started_unlocked()
            return {
                "ok": True,
                "action": "started",
                "pid": proc.pid,
                "cmd": self._build_cmd(),
                "stdioLog": str(POLLER_STDIO_LOG_PATH),
            }

    def stop(self, timeout_s: float = 6.0) -> dict[str, Any]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                return {"ok": True, "action": "already_stopped"}

            proc = self._proc
            pid = proc.pid
            try:
                proc.terminate()
                proc.wait(timeout=timeout_s)
                stopped = True
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                    stopped = True
                except Exception:
                    stopped = False
            except Exception:
                stopped = False

            self._proc = None
            return {"ok": stopped, "action": "stopped", "pid": pid}

    def restart(self) -> dict[str, Any]:
        stop_res = self.stop()
        if not stop_res.get("ok", True):
            return {"ok": False, "action": "restart_failed", "stop": stop_res}
        start_res = self.start()
        return {"ok": True, "action": "restarted", "stop": stop_res, "start": start_res}


_poller_manager = PollerManager()


def restart_poller() -> dict[str, Any]:
    strategy = (os.environ.get("POE_POLLER_RESTART_STRATEGY") or "auto").strip().lower()
    systemd_service = (os.environ.get("POE_POLLER_SYSTEMD_SERVICE") or _DEFAULT_SYSTEMD_POLLER_SERVICE).strip()

    # Production (VPS) expectation: poller is managed by systemd.
    if strategy in {"auto", "systemd"} and os.name != "nt":
        res = _restart_poller_systemd(systemd_service)
        if res.get("ok") or strategy == "systemd":
            if res.get("ok"):
                _append_poller_log("[admin] Poller restarted (systemd).")
            return res
        # auto fallback if systemd restart fails

    # Local/dev fallback: stop any externally-started pollers, then restart the server-owned one.
    external = _kill_external_pollers()
    managed = _poller_manager.restart()
    if managed.get("ok") and not external.get("errors"):
        killed = external.get("killedPids") or []
        suffix = f" (killed {len(killed)} existing process(es))" if killed else ""
        _append_poller_log(f"[admin] Poller restarted.{suffix}")
    return {
        "ok": bool(managed.get("ok")),
        "action": "restarted",
        "mode": "subprocess",
        "external": external,
        "managed": managed,
    }


def stop_poller() -> dict[str, Any]:
    strategy = (os.environ.get("POE_POLLER_RESTART_STRATEGY") or "auto").strip().lower()
    systemd_service = (os.environ.get("POE_POLLER_SYSTEMD_SERVICE") or _DEFAULT_SYSTEMD_POLLER_SERVICE).strip()

    if strategy in {"auto", "systemd"} and os.name != "nt":
        res = _stop_poller_systemd(systemd_service)
        if res.get("ok") or strategy == "systemd":
            if res.get("ok"):
                _append_poller_log("[admin] Poller stopped (systemd).")
            return res

    # Local/dev: kill any pollers + stop server-owned poller if it exists.
    external = _kill_external_pollers()
    managed = _poller_manager.stop()
    if managed.get("ok", True) and not external.get("errors"):
        killed = external.get("killedPids") or []
        suffix = f" (killed {len(killed)} existing process(es))" if killed else ""
        _append_poller_log(f"[admin] Poller stopped.{suffix}")
    return {
        "ok": bool(managed.get("ok", True)) and not external.get("errors"),
        "action": "stopped",
        "mode": "subprocess",
        "external": external,
        "managed": managed,
    }


def poller_autostart_enabled() -> bool:
    return (os.environ.get("POE_POLLER_AUTOSTART") or "").strip().lower() in {"1", "true", "yes"}

_DEFAULT_PRICE_POLL_HEADER = (
    "timestamp_utc,cycle,item_name,item_mode,query_id,total_results,used_results,"
    "unsupported_price_count,mirror_count,lowest_mirror,median_mirror,highest_mirror,"
    "divine_count,lowest_divine,median_divine,highest_divine\n"
)


def _load_geo_cache_unlocked() -> dict[str, dict[str, Any]]:
    global _geo_cache
    if _geo_cache is not None:
        return _geo_cache
    if IP_GEO_CACHE_PATH.exists():
        try:
            with IP_GEO_CACHE_PATH.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            _geo_cache = raw if isinstance(raw, dict) else {}
        except Exception:
            _geo_cache = {}
    else:
        _geo_cache = {}
    return _geo_cache


def _save_geo_cache(data: dict[str, dict[str, Any]]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with IP_GEO_CACHE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def is_private_or_reserved_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    )


def get_client_ip(x_forwarded_for: str | None, remote_addr: str) -> str:
    if x_forwarded_for:
        first = x_forwarded_for.split(",")[0].strip()
        if first:
            return first
    return remote_addr.split(":")[0] if remote_addr else ""


def visitors_include_local_ips() -> bool:
    return os.environ.get("POE_VISITORS_INCLUDE_LOCAL", "").strip().lower() in {"1", "true", "yes"}


def skip_ip_in_visitor_stats(ip: str) -> bool:
    """Drop loopback/private unless POE_VISITORS_INCLUDE_LOCAL=1 (so local dev can see 127.0.0.1)."""
    if not ip:
        return True
    if visitors_include_local_ips():
        return False
    return is_private_or_reserved_ip(ip)


def record_site_visit(ip: str, path: str) -> None:
    if skip_ip_in_visitor_stats(ip):
        return
    if path not in {"/", "/index.html"}:
        return
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ip": ip,
            "path": path,
        },
        ensure_ascii=False,
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _visit_lock:
        with VISITORS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def tail_log_file(path: Path, max_bytes: int = 262_144) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
        return data
    except OSError:
        return ""


def read_log_file_since(path: Path, cursor: int, max_bytes: int = 262_144) -> tuple[str, int]:
    """
    Read newly appended bytes since `cursor` (a file byte offset).
    Returns (text, new_cursor). If the file shrank or cursor is invalid, starts from 0.
    """
    if not path.exists():
        return "", 0
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if cursor < 0 or cursor > size:
                cursor = 0
            fh.seek(cursor)
            remaining = size - cursor
            if remaining <= 0:
                return "", size
            data = fh.read(min(remaining, max_bytes)).decode("utf-8", errors="replace")
        return data, size
    except OSError:
        return "", 0


def _parse_jsonl_logs(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if not raw.startswith("{"):
            # Mixed log files can contain older plain text lines; ignore them.
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        out.append(row)
    return out


def query_log_entries(
    path: Path,
    *,
    limit: int = 2000,
    level: str = "all",
    q: str = "",
    max_bytes: int = 1_048_576,
    cursor: int | None = None,
    include_counts: bool = True,
    since: str = "session",
) -> dict[str, Any]:
    """
    Return structured log entries + level counts for the tailed window.

    Levels are the standard python logging names lowercased (info/warning/error/critical),
    but the UI uses: all/info/warn/error. We normalize 'warn' -> 'warning'.
    """
    wanted = (level or "all").strip().lower()
    if wanted == "warn":
        wanted = "warning"
    query = (q or "").strip().lower()

    def include(e: dict[str, Any]) -> bool:
        if wanted != "all":
            lvl = str(e.get("level") or "").lower()
            if lvl == "warn":
                lvl = "warning"
            if lvl != wanted:
                return False
        if query:
            msg = str(e.get("msg") or "").lower()
            name = str(e.get("name") or "").lower()
            exc = str(e.get("exc") or "").lower()
            if query not in msg and query not in name and query not in exc:
                return False
        return True

    limit = max(1, min(int(limit or 2000), 5000))
    since_mode = (since or "session").strip().lower()

    # Snapshot parsing (for counts + initial render).
    snapshot_entries: list[dict[str, Any]] = []
    counts: dict[str, int] | None = None
    file_cursor: int | None = None
    if include_counts or cursor is None:
        raw = tail_log_file(path, max_bytes=max_bytes)
        snapshot_entries = _parse_jsonl_logs(raw)
        if not snapshot_entries:
            return {
                "format": "text",
                "text": raw,
                "counts": {},
                "entries": [],
                "cursor": 0,
            }
        snapshot_entries = snapshot_entries[-limit:]

        if since_mode == "session":
            last_start_idx: int | None = None
            for idx, e in enumerate(snapshot_entries):
                if str(e.get("event") or "") == "session_start":
                    last_start_idx = idx
            if last_start_idx is not None:
                snapshot_entries = snapshot_entries[last_start_idx + 1 :]

        if include_counts:
            counts = {"info": 0, "warning": 0, "error": 0, "other": 0, "all": 0}
            for e in snapshot_entries:
                lvl = str(e.get("level") or "").lower()
                counts["all"] += 1
                if lvl == "info":
                    counts["info"] += 1
                elif lvl in {"warn", "warning"}:
                    counts["warning"] += 1
                elif lvl == "error":
                    counts["error"] += 1
                else:
                    counts["other"] += 1

    # Incremental parsing (only new bytes).
    if cursor is not None:
        delta_text, file_cursor = read_log_file_since(path, cursor, max_bytes=262_144)
        delta_entries = _parse_jsonl_logs(delta_text)
        filtered = [e for e in delta_entries if isinstance(e, dict) and include(e)]
        return {
            "format": "jsonl",
            "counts": counts,
            "entries": filtered,
            "limit": limit,
            "cursor": file_cursor or 0,
            "delta": True,
            "since": since_mode,
        }

    # Full response (filtered snapshot).
    filtered_snapshot = [e for e in snapshot_entries if isinstance(e, dict) and include(e)]
    # Best-effort cursor for subsequent incremental polls.
    try:
        file_cursor = path.stat().st_size
    except OSError:
        file_cursor = 0
    return {
        "format": "jsonl",
        "counts": counts,
        "entries": filtered_snapshot,
        "limit": limit,
        "cursor": file_cursor or 0,
        "delta": False,
        "since": since_mode,
    }


def _fetch_geo_ip(ip: str) -> dict[str, Any] | None:
    if is_private_or_reserved_ip(ip):
        return None
    url = f"http://ip-api.com/json/{ip}?fields=status,message,lat,lon,query"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "poe-market-flips-admin/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    lat, lon = data.get("lat"), data.get("lon")
    if lat is None or lon is None:
        return None
    try:
        return {"lat": float(lat), "lon": float(lon), "ip": ip}
    except (TypeError, ValueError):
        return None


def visitor_map_payload() -> dict[str, Any]:
    ip_counts: dict[str, int] = {}
    ip_last: dict[str, str] = {}
    if VISITORS_PATH.exists():
        try:
            with VISITORS_PATH.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ip = str(row.get("ip") or "").strip()
                    if not ip or skip_ip_in_visitor_stats(ip):
                        continue
                    ip_counts[ip] = ip_counts.get(ip, 0) + 1
                    ts = str(row.get("ts") or "")
                    if ts:
                        ip_last[ip] = ts
        except OSError:
            pass

    with _geo_lock:
        cache = _load_geo_cache_unlocked()
        points: list[dict[str, Any]] = []
        max_new_lookups = 40
        lookups_done = 0
        sorted_ips = sorted(ip_counts.items(), key=lambda x: (-x[1], x[0]))
        for ip, count in sorted_ips:
            entry = cache.get(ip)
            if entry is None and lookups_done < max_new_lookups:
                if lookups_done > 0:
                    time.sleep(1.35)
                geo = _fetch_geo_ip(ip)
                lookups_done += 1
                if geo:
                    entry = {"lat": geo["lat"], "lon": geo["lon"]}
                    cache[ip] = entry
                    _save_geo_cache(cache)
            if not entry:
                continue
            lat = float(entry["lat"])
            lon = float(entry["lon"])
            points.append(
                {
                    "lat": lat,
                    "lng": lon,
                    "weight": float(count),
                    "ip": ip,
                    "visits": count,
                    "lastSeen": ip_last.get(ip),
                }
            )

        pending_geocodes = sum(1 for ip in ip_counts if ip not in cache)

    visitor_rows = [
        {
            "ip": ip,
            "visits": ip_counts[ip],
            "lastSeen": ip_last.get(ip),
            "onMap": ip in cache,
        }
        for ip in sorted(ip_counts.keys(), key=lambda x: (-ip_counts[x], x))
    ]

    return {
        "points": points,
        "uniqueVisitors": len(ip_counts),
        "totalVisits": sum(ip_counts.values()),
        "pendingGeocodes": pending_geocodes,
        "visitors": visitor_rows,
    }


def csv_download_headers() -> tuple[str, Path]:
    filename = CSV_PATH.name
    return filename, CSV_PATH


def clear_market_data(*, listings_cache_path: Path, csv_path: Path) -> dict[str, Any]:
    """
    Clear local cache + CSV history.

    - listings_cache.json: delete if present (dashboard will treat as cache-miss)
    - price_poll.csv: truncate to header only (preserves column names for downstream readers)
    """
    cleared_cache = False
    cleared_csv = False

    try:
        if listings_cache_path.exists():
            listings_cache_path.unlink()
        cleared_cache = True
    except OSError:
        cleared_cache = False

    header = _DEFAULT_PRICE_POLL_HEADER
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                first_line = fh.readline()
            if first_line.strip():
                header = first_line.rstrip("\n") + "\n"
        except OSError:
            pass

    try:
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(header)
        cleared_csv = True
    except OSError:
        cleared_csv = False

    return {
        "cleared": {
            "listingsCache": cleared_cache,
            "pricePollCsv": cleared_csv,
        }
    }


def admin_session_cookie_value() -> str:
    """HMAC derived from ADMIN_TOKEN; stored in HttpOnly cookie (not the raw token)."""
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not token:
        return ""
    return hmac.new(token.encode("utf-8"), b"poe-admin-session-v1", hashlib.sha256).hexdigest()


def should_issue_admin_session_cookie(query_token: str | None) -> bool:
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    return bool(expected and query_token and query_token.strip() == expected)


def build_admin_session_set_cookie(x_forwarded_proto: str | None) -> str:
    """Set-Cookie value for admin_session after successful ?token= exchange."""
    digest = admin_session_cookie_value()
    if not digest:
        return ""
    secure = (
        "; Secure"
        if (x_forwarded_proto or "").strip().lower() == "https"
        else ""
    )
    return (
        f"admin_session={digest}; HttpOnly; Path=/; Max-Age=2592000; SameSite=Lax{secure}"
    )


def _cookie_value_for_name(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    prefix = f"{name}="
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix) :].strip().strip('"')
    return None


def admin_authorized(
    auth_header: str | None,
    query_token: str | None,
    cookie_header: str | None = None,
) -> bool:
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not token:
        return True
    if query_token and query_token.strip() == token:
        return True
    if auth_header and auth_header.startswith("Bearer "):
        if auth_header[7:].strip() == token:
            return True
    expected_cookie = admin_session_cookie_value()
    if expected_cookie:
        got = _cookie_value_for_name(cookie_header, "admin_session")
        if got and hmac.compare_digest(got, expected_cookie):
            return True
    return False
