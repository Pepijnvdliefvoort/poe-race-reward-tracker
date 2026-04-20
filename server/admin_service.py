from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
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
VISITORS_PATH = LOG_DIR / "visitors.jsonl"
IP_GEO_CACHE_PATH = LOG_DIR / "ip_geo_cache.json"

_visit_lock = threading.Lock()
_geo_lock = threading.Lock()
_geo_cache: dict[str, dict[str, Any]] | None = None


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
