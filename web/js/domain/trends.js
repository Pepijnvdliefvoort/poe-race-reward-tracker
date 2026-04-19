import { MAX_POINTS, THREE_MONTHS_MS } from "../core/state.js";
import { getCondensedChartPoints } from "../core/utils.js";

export function getTrendValue(item) {
  const cutoff = Date.now() - THREE_MONTHS_MS;
  const rawPoints = (item?.points || []).filter((p) => p.time >= cutoff);
  const chartPoints = getCondensedChartPoints(rawPoints, MAX_POINTS);
  const valid = chartPoints.map((p) => p.y).filter((v) => v != null && !Number.isNaN(v));

  if (valid.length >= 2) {
    const first = valid[0];
    const last = valid[valid.length - 1];
    return last - first;
  }
  return 0;
}

export function getTrendPercentage(item) {
  const cutoff = Date.now() - THREE_MONTHS_MS;
  const rawPoints = (item?.points || []).filter((p) => p.time >= cutoff);
  const chartPoints = getCondensedChartPoints(rawPoints, MAX_POINTS);
  const valid = chartPoints.map((p) => p.y).filter((v) => v != null && !Number.isNaN(v));

  if (valid.length >= 2) {
    const first = valid[0];
    const last = valid[valid.length - 1];
    if (first === 0) return 0;
    return ((last - first) / first) * 100;
  }
  return 0;
}

export function getTrendDirection(item) {
  const percentage = getTrendPercentage(item);
  const rounded = Math.round(percentage);
  if (rounded > 0) return "up";
  if (rounded < 0) return "down";
  return "flat";
}

