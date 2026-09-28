from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Dashboard polls every 30s; keep server cache alive longer so refreshes usually hit RAM.
TTL_SECONDS_FULL = 120.0
TTL_SECONDS_WINDOW = 60.0
# Bucket sinceMs so cache keys stay stable across periodic refreshes.
# Keep this small: the payload's inferenceWindow cutoff derives from the bucketed value.
BUCKET_MS = 30 * 60 * 1000
# sinceMs is client-supplied on a public endpoint: clamp it to the longest window the dashboard
# requests (730d custom cap + 2d buffer, with headroom) and cap concurrent cache-miss builds.
MAX_SINCE_AGE_MS = 740 * 24 * 60 * 60 * 1000
_MAX_CONCURRENT_BUILDS = 2
_build_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_BUILDS)
_MAX_ENTRIES = 32

_lock = threading.Lock()
_cache: dict[str, tuple[float, bytes]] = {}
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _bucketed_since_ms(since_ms: int | None) -> int:
    now_ms = int(time.time() * 1000)
    ms = int(since_ms or 0)
    ms = min(max(ms, now_ms - MAX_SINCE_AGE_MS), now_ms)
    return (ms // BUCKET_MS) * BUCKET_MS


def prices_cache_key(*, full_history: bool, since_ms: int | None) -> str:
    if full_history:
        return "full"
    return f"since:{_bucketed_since_ms(since_ms)}"


def since_ms_for_load(*, full_history: bool, since_ms: int | None) -> int | None:
    """Normalize cutoff for DB load so all clients in the same bucket share one payload."""
    if full_history:
        return None
    return _bucketed_since_ms(since_ms)


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
    # Drop idle build locks for keys no longer cached so the dict can't grow without bound.
    with _build_locks_guard:
        for k in [k for k, lock in _build_locks.items() if k not in _cache and not lock.locked()]:
            del _build_locks[k]


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

        with _build_slots:
            body = build()
        with _lock:
            _cache[key] = (time.monotonic() + ttl, body)
            _prune_locked(now)
        return body
