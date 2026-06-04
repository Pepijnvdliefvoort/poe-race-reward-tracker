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
 * Prefer server-precomputed totals when present (accurate after poll downsampling).
 * @param {object} item - API item row
 * @param {number} spanMs - getChartTimespanMs(); Infinity means entire history
 */
export function aggregateInferenceSignalsForItem(item, spanMs) {
  const precomputed = item?.inferenceWindow;
  if (precomputed && typeof precomputed === "object") {
    return precomputed;
  }
  return aggregateInferenceSignalsOverWindow(item?.points, spanMs);
}

/**
 * @param {object[]} points - item.points from API
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
    nonInstOnline: sumKey("inferenceLikelyNonInstantOnline"),
    relist: sumKey("inferenceRelistSameSeller"),
    nib: sumKey("inferenceNonInstantRemoved"),
    repr: sumKey("inferenceRepriceSameSeller"),
    multi: sumKey("inferenceMultiSellerSameFingerprint"),
    newRows: sumKey("inferenceNewListingRows"),
  };
}

function salesInTimeWindow(sale, cutoffMs) {
  const t = sale?.time;
  if (t == null || Number.isNaN(t)) {
    return false;
  }
  if (cutoffMs == null) {
    return true;
  }
  return t >= cutoffMs;
}

/**
 * Count visible sale rows over the active chart window.
 * This matches the sales chart source (non-reverted rows from backend).
 */
export function countSalesRowsOverWindow(sales, spanMs) {
  const now = Date.now();
  const cutoffMs =
    typeof spanMs === "number" && Number.isFinite(spanMs) && spanMs !== Infinity ? now - spanMs : null;
  return (sales || []).filter((s) => salesInTimeWindow(s, cutoffMs)).length;
}

/**
 * Rough sold count: confirmed transfers + likely instant sales + non-instant/online heuristic
 * (same signals as the rules engine). Other counters are excluded — not treated as sales.
 */
export function estimatedSoldCount(agg) {
  if (!agg) {
    return 0;
  }
  return (
    (Number(agg.xfer) || 0) +
    (Number(agg.instant) || 0) +
    (Number(agg.nonInstOnline) || 0)
  );
}

/**
 * Preferred estimator for UI consistency: count visible sales rows in-window.
 * Falls back to poll-level inference counters when sales rows are unavailable.
 */
export function estimatedSoldForItemWindow(item, spanMs) {
  const salesRows = item?.sales;
  if (Array.isArray(salesRows)) {
    return countSalesRowsOverWindow(salesRows, spanMs);
  }
  const agg = aggregateInferenceSignalsForItem(item, spanMs);
  return estimatedSoldCount(agg);
}

export function formatEstimatedSoldLine(agg) {
  if (!agg || agg.pollsInWindow <= 0) {
    return "";
  }
  const n = estimatedSoldCount(agg);
  return `Est. sold: ~${n}`;
}
