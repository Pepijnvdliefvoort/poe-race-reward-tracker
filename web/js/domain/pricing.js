import { THREE_MONTHS_MS } from "../core/state.js";

export function getAvailableLowestPrice(item) {
  const latest = item?.latest;
  if (!latest?.time || Date.now() - latest.time >= THREE_MONTHS_MS) {
    return null;
  }

  const low = latest.lowestMirror;
  return low == null || Number.isNaN(low) ? null : low;
}

