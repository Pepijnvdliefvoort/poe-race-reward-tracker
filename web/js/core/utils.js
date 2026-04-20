export function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "n/a";
  }
  if (Math.abs(value) >= 1000) {
    return value.toFixed(0);
  }
  return value.toFixed(2).replace(/\.00$/, "");
}

/**
 * Format Unix epoch milliseconds (UTC-based) to local time string.
 * Input: epoch milliseconds from backend (always UTC-based)
 * Output: formatted time string in browser's local timezone via toLocaleTimeString()
 * Example: formatTime(1713275400000) -> "2:30 PM" (in user's local timezone)
 */
export function formatTime(ms, withSeconds = false) {
  const d = new Date(ms);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined,
  });
}

export function getPointMirrorValue(point) {
  const value = point?.medianMirror ?? point?.lowestMirror ?? point?.highestMirror;
  return value == null || Number.isNaN(value) ? null : value;
}

function applyCarryForward(points, carryForwardMs) {
  if (!points.length) return points;
  const out = [];
  let lastY = null;
  let lastT = null;
  for (const p of points) {
    const t = p.x;
    let y = p.y;
    if ((y == null || Number.isNaN(y)) && lastY != null && lastT != null) {
      const gap = t - lastT;
      if (gap > 0 && gap <= carryForwardMs) {
        y = lastY;
      } else {
        y = null;
      }
    }
    if (y != null && !Number.isNaN(y)) {
      lastY = y;
      lastT = t;
    }
    out.push({ x: t, y });
  }
  return out;
}

function applyEma(points, halfLifeMs) {
  if (!points.length) return points;
  if (!halfLifeMs || halfLifeMs <= 0) return points;

  const out = [];
  let ema = null;
  let lastT = null;

  for (const p of points) {
    const t = p.x;
    const y = p.y;

    if (y == null || Number.isNaN(y)) {
      out.push({ x: t, y: null });
      continue;
    }

    if (ema == null || lastT == null) {
      ema = y;
      lastT = t;
      out.push({ x: t, y: ema });
      continue;
    }

    const dt = Math.max(0, t - lastT);
    const alpha = 1 - Math.pow(0.5, dt / halfLifeMs);
    ema = ema + alpha * (y - ema);
    lastT = t;
    out.push({ x: t, y: ema });
  }

  return out;
}

// LTTB keeps the overall line shape when reducing many points to a smaller, readable set.
export function largestTriangleThreeBuckets(data, threshold) {
  if (threshold >= data.length || threshold === 0) {
    return data;
  }

  const sampled = [data[0]];
  const every = (data.length - 2) / (threshold - 2);
  let a = 0;

  for (let i = 0; i < threshold - 2; i += 1) {
    let avgX = 0;
    let avgY = 0;
    const avgRangeStart = Math.floor((i + 1) * every) + 1;
    const avgRangeEnd = Math.min(Math.floor((i + 2) * every) + 1, data.length);
    const avgRangeLength = Math.max(1, avgRangeEnd - avgRangeStart);

    for (let j = avgRangeStart; j < avgRangeEnd; j += 1) {
      avgX += data[j].x;
      avgY += data[j].y;
    }
    avgX /= avgRangeLength;
    avgY /= avgRangeLength;

    const rangeOffs = Math.floor(i * every) + 1;
    const rangeTo = Math.min(Math.floor((i + 1) * every) + 1, data.length - 1);

    let maxArea = -1;
    let maxAreaPoint = data[rangeOffs] ?? data[a];
    let nextA = rangeOffs;

    for (let j = rangeOffs; j < rangeTo; j += 1) {
      const area = Math.abs(
        (data[a].x - avgX) * (data[j].y - data[a].y) -
        (data[a].x - data[j].x) * (avgY - data[a].y)
      ) * 0.5;

      if (area > maxArea) {
        maxArea = area;
        maxAreaPoint = data[j];
        nextA = j;
      }
    }

    sampled.push(maxAreaPoint);
    a = nextA;
  }

  sampled.push(data[data.length - 1]);
  return sampled;
}

export function getCondensedChartPoints(points, maxPoints) {
  const normalized = points
    .map((point) => ({ x: point.time, y: getPointMirrorValue(point) }))
    .sort((a, b) => a.x - b.x);

  // Stabilize thin markets:
  // - carry-forward small gaps (seller offline / API misses) so we don't create artificial spikes
  // - apply EMA to reduce hour-to-hour wobble while still tracking real moves
  const carried = applyCarryForward(normalized, 6 * 60 * 60 * 1000);
  const smoothed = applyEma(carried, 8 * 60 * 60 * 1000);
  const kept = smoothed.filter((point) => point.y != null);

  if (kept.length <= maxPoints) {
    return kept;
  }

  return largestTriangleThreeBuckets(kept, maxPoints);
}

function inferStepMs(actualPoints) {
  if (!actualPoints || actualPoints.length < 2) return 60 * 60 * 1000;
  const deltas = [];
  for (let i = Math.max(1, actualPoints.length - 6); i < actualPoints.length; i += 1) {
    const dt = actualPoints[i].x - actualPoints[i - 1].x;
    if (Number.isFinite(dt) && dt > 0) deltas.push(dt);
  }
  deltas.sort((a, b) => a - b);
  if (!deltas.length) return 60 * 60 * 1000;
  return deltas[Math.floor(deltas.length / 2)];
}

function linearFitSlope(points) {
  // Fit y = a + b*t with t starting at 0 to avoid huge X values.
  if (!points || points.length < 2) return 0;
  const t0 = points[0].x;
  let n = 0;
  let sumT = 0;
  let sumY = 0;
  let sumTT = 0;
  let sumTY = 0;
  for (const p of points) {
    const t = (p.x - t0) / (60 * 60 * 1000); // hours
    const y = p.y;
    if (!Number.isFinite(t) || !Number.isFinite(y)) continue;
    n += 1;
    sumT += t;
    sumY += y;
    sumTT += t * t;
    sumTY += t * y;
  }
  const denom = n * sumTT - sumT * sumT;
  if (n < 2 || denom === 0) return 0;
  return (n * sumTY - sumT * sumY) / denom; // mirrors per hour
}

export function getChartSeriesWithPrediction(points, maxActualPoints, predictionPoints = 1) {
  const actual = getCondensedChartPoints(points, maxActualPoints);
  if (!actual.length || predictionPoints <= 0) {
    return { actual, predicted: [] };
  }

  const stepMs = inferStepMs(actual);
  const fitWindow = actual.slice(Math.max(0, actual.length - 6));
  const slopePerHour = linearFitSlope(fitWindow);
  const stepHours = stepMs / (60 * 60 * 1000);

  const last = actual[actual.length - 1];
  const predicted = [];
  for (let i = 1; i <= predictionPoints; i += 1) {
    const x = last.x + stepMs * i;
    const y = Math.max(0, last.y + slopePerHour * stepHours * i);
    predicted.push({ x, y });
  }

  return { actual, predicted };
}
