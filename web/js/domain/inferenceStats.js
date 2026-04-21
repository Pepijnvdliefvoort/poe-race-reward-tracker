/**
 * Aggregate sale-inference counters from poll rows over the same window as the price chart.
 */

function inTimeWindow(point, cutoffMs) {
  const t = point?.time;
  if (t == null || Number.isNaN(t)) {
    return false;
  }
  if (cutoffMs == null) {
    return true;
  }
  return t >= cutoffMs;
}

/**
 * @param {object[]} points - item.points from API (full series)
 * @param {number} spanMs - getChartTimespanMs(); Infinity means entire history
 */
export function aggregateInferenceSignalsOverWindow(points, spanMs) {
  const now = Date.now();
  const cutoffMs =
    typeof spanMs === "number" && Number.isFinite(spanMs) && spanMs !== Infinity ? now - spanMs : null;

  const list = (points || []).filter((p) => inTimeWindow(p, cutoffMs));

  const sumKey = (key) => list.reduce((acc, p) => acc + (Number(p[key]) || 0), 0);

  return {
    pollsInWindow: list.length,
    xfer: sumKey("inferenceConfirmedTransfer"),
    instant: sumKey("inferenceLikelyInstantSale"),
    relist: sumKey("inferenceRelistSameSeller"),
    nib: sumKey("inferenceNonInstantRemoved"),
    repr: sumKey("inferenceRepriceSameSeller"),
    multi: sumKey("inferenceMultiSellerSameFingerprint"),
    newRows: sumKey("inferenceNewListingRows"),
  };
}

/**
 * Rough sold count: confirmed transfers + likely instant sales (same signals as the rules engine).
 * Other counters (relist, reprice, etc.) are excluded — they are not treated as sales.
 */
export function estimatedSoldCount(agg) {
  if (!agg) {
    return 0;
  }
  return (Number(agg.xfer) || 0) + (Number(agg.instant) || 0);
}

export function formatEstimatedSoldLine(agg) {
  if (!agg || agg.pollsInWindow <= 0) {
    return "";
  }
  const n = estimatedSoldCount(agg);
  return `Est. sold: ~${n}`;
}
