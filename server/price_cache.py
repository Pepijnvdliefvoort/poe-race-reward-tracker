from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Dashboard polls every 30s; keep server cache alive longer so refreshes usually hit RAM.
TTL_SECONDS_FULL = 120.0
TTL_SECONDS_WINDOW = 60.0
# Bucket sinceMs so cache keys stay stable across periodic refreshes.
BUCKET_MS = 30 * 60 * 1000
_MAX_ENTRIES = 32

_lock = threading.Lock()
_cache: dict[str, tuple[float, bytes]] = {}
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def prices_cache_key(*, full_history: bool, since_ms: int | None) -> str:
    if full_history:
        return "full"
    ms = int(since_ms or 0)
    bucket = (ms // BUCKET_MS) * BUCKET_MS
    return f"since:{bucket}"


def since_ms_for_load(*, full_history: bool, since_ms: int | None) -> int | None:
    """Normalize cutoff for DB load so all clients in the same bucket share one payload."""
    if full_history:
        return None
    ms = int(since_ms or 0)
    return (ms // BUCKET_MS) * BUCKET_MS


def _ttl_seconds(key: str) -> float:
    return TTL_SECONDS_FULL if key == "full" else TTL_SECONDS_WINDOW


def _build_lock_for(key: str) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_locks[key] = lock
        return lock


def _prune_locked(now: float) -> None:
    expired = [k for k, (exp, _) in _cache.items() if now >= exp]
    for k in expired:
        del _cache[k]
    while len(_cache) > _MAX_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest_key]


def get_cached_prices_body(
    *,
    full_history: bool,
    since_ms: int | None,
    build: Callable[[], bytes],
) -> bytes:
    key = prices_cache_key(full_history=full_history, since_ms=since_ms)
    ttl = _ttl_seconds(key)
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            expires, body = entry
            if now < expires:
                return body

    with _build_lock_for(key):
        with _lock:
            entry = _cache.get(key)
            if entry is not None:
                expires, body = entry
                if now < expires:
                    return body

        body = build()
        with _lock:
            _prune_locked(now)
            _cache[key] = (time.monotonic() + ttl, body)
        return body
