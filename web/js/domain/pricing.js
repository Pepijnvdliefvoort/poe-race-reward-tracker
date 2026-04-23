import { THREE_MONTHS_MS } from "../core/state.js";

function pickMirrorField(latest, keys) {
  if (!latest) return null;
  for (const k of keys) {
    const v = latest[k];
    if (v != null && !Number.isNaN(v)) return v;
  }
  return null;
}

/** Lowest mirror for display / sorting; uses last poll, then last-known chain from API. */
export function getDisplayLowestMirror(latest) {
  return pickMirrorField(latest, [
    "lowestMirror",
    "lastKnownLowestMirror",
    "lastKnownMedianMirror",
    "lastKnownHighestMirror",
  ]);
}

/** Highest mirror for card range; prefers live poll, then last-known chain. */
export function getDisplayHighestMirror(latest) {
  return pickMirrorField(latest, [
    "highestMirror",
    "lastKnownHighestMirror",
    "lastKnownMedianMirror",
    "lastKnownLowestMirror",
  ]);
}

/** True when we are showing a carried-forward price (no live lowest this poll). */
export function isShowingLastKnownMirrorPrice(latest) {
  const live = latest?.lowestMirror;
  const hasLive = live != null && !Number.isNaN(live);
  return Boolean(latest && !hasLive && getDisplayLowestMirror(latest) != null);
}

export function getAvailableLowestPrice(item) {
  const latest = item?.latest;
  if (!latest?.time || Date.now() - latest.time >= THREE_MONTHS_MS) {
    return null;
  }

  const low = getDisplayLowestMirror(latest);
  return low == null || Number.isNaN(low) ? null : low;
}

