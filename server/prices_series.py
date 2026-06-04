"""Downsample price poll series and pre-aggregate inference counters for /api/prices."""
from __future__ import annotations

import math
import os
from typing import Any

# Keep in sync with web/js/core/state.js PRICES_HISTORY_BUFFER_MS
PRICES_HISTORY_BUFFER_MS = 2 * 24 * 60 * 60 * 1000

CHART_MAX_POINTS = int(os.getenv("POE_PRICES_CHART_MAX_POINTS", "250"))

_INFERENCE_KEYS = (
    "inferenceConfirmedTransfer",
    "inferenceLikelyInstantSale",
    "inferenceLikelyNonInstantOnline",
    "inferenceRelistSameSeller",
    "inferenceNonInstantRemoved",
    "inferenceRepriceSameSeller",
    "inferenceMultiSellerSameFingerprint",
    "inferenceNewListingRows",
)


def chart_mirror_value(point: dict[str, Any]) -> float | None:
    for key in ("medianMirror", "lowestMirror", "highestMirror"):
        raw = point.get(key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val):
            return val
    return None


def aggregate_inference_window(points: list[dict[str, Any]]) -> dict[str, int]:
    """Same shape as web/js/domain/inferenceStats.js aggregateInferenceSignalsOverWindow."""
    return {
        "pollsInWindow": len(points),
        "xfer": sum(int(p.get("inferenceConfirmedTransfer") or 0) for p in points),
        "instant": sum(int(p.get("inferenceLikelyInstantSale") or 0) for p in points),
        "nonInstOnline": sum(int(p.get("inferenceLikelyNonInstantOnline") or 0) for p in points),
        "relist": sum(int(p.get("inferenceRelistSameSeller") or 0) for p in points),
        "nib": sum(int(p.get("inferenceNonInstantRemoved") or 0) for p in points),
        "repr": sum(int(p.get("inferenceRepriceSameSeller") or 0) for p in points),
        "multi": sum(int(p.get("inferenceMultiSellerSameFingerprint") or 0) for p in points),
        "newRows": sum(int(p.get("inferenceNewListingRows") or 0) for p in points),
    }


def _lttb_indices(xs: list[float], ys: list[float], threshold: int) -> list[int]:
    """Largest-Triangle-Three-Buckets index selection (matches web/js/core/utils.js)."""
    n = len(xs)
    if threshold >= n or threshold <= 0:
        return list(range(n))
    if threshold == 1:
        return [0]

    sampled = [0]
    every = (n - 2) / (threshold - 2)
    a = 0

    for i in range(threshold - 2):
        avg_range_start = int((i + 1) * every) + 1
        avg_range_end = min(int((i + 2) * every) + 1, n)
        avg_range_length = max(1, avg_range_end - avg_range_start)
        avg_x = sum(xs[j] for j in range(avg_range_start, avg_range_end)) / avg_range_length
        avg_y = sum(ys[j] for j in range(avg_range_start, avg_range_end)) / avg_range_length

        range_offs = int(i * every) + 1
        range_to = min(int((i + 1) * every) + 1, n - 1)

        max_area = -1.0
        max_idx = range_offs
        next_a = range_offs
        ax = xs[a]
        ay = ys[a]

        for j in range(range_offs, range_to):
            area = abs((ax - avg_x) * (ys[j] - ay) - (ax - xs[j]) * (avg_y - ay)) * 0.5
            if area > max_area:
                max_area = area
                max_idx = j
                next_a = j

        sampled.append(max_idx)
        a = next_a

    sampled.append(n - 1)
    return sampled


def _uniform_stride_indices(length: int, threshold: int) -> list[int]:
    if length <= threshold:
        return list(range(length))
    if threshold <= 1:
        return [0]
    step = (length - 1) / (threshold - 1)
    out = [min(length - 1, int(round(i * step))) for i in range(threshold)]
    return sorted(set(out))


def downsample_chart_points(
    points: list[dict[str, Any]],
    max_points: int = CHART_MAX_POINTS,
) -> list[dict[str, Any]]:
    if max_points <= 0 or len(points) <= max_points:
        return points

    mirror_idx: list[int] = []
    xs: list[float] = []
    ys: list[float] = []
    for i, p in enumerate(points):
        y = chart_mirror_value(p)
        if y is None:
            continue
        mirror_idx.append(i)
        xs.append(float(p["time"]))
        ys.append(y)

    if len(mirror_idx) < 2:
        keep = _uniform_stride_indices(len(points), max_points)
        return [points[i] for i in keep]

    picked = set(_lttb_indices(xs, ys, max_points))
    picked.add(0)
    picked.add(len(mirror_idx) - 1)
    if len(picked) > max_points:
        picked = set(_uniform_stride_indices(len(mirror_idx), max_points))

    out_indices = sorted(mirror_idx[i] for i in picked)
    return [points[i] for i in out_indices]


def chart_inference_cutoff_ms(*, since_ms: int | None, full_history: bool) -> int | None:
    """Epoch ms for chart-span inference (excludes pre-window buffer rows)."""
    if full_history or since_ms is None:
        return None
    return int(since_ms) + PRICES_HISTORY_BUFFER_MS


def apply_chart_series_limits(
    item: dict[str, Any],
    *,
    since_ms: int | None,
    full_history: bool,
    max_points: int = CHART_MAX_POINTS,
) -> None:
    points = list(item.get("points") or [])
    cutoff = chart_inference_cutoff_ms(since_ms=since_ms, full_history=full_history)
    if cutoff is None:
        inference_points = points
    else:
        inference_points = [p for p in points if int(p.get("time") or 0) >= cutoff]
    item["inferenceWindow"] = aggregate_inference_window(inference_points)
    item["points"] = downsample_chart_points(points, max_points)
