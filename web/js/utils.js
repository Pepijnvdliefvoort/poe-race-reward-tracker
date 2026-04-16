export function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "n/a";
  }
  if (Math.abs(value) >= 1000) {
    return value.toFixed(0);
  }
  return value.toFixed(2).replace(/\.00$/, "");
}

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
    .filter((point) => point.y != null);

  if (normalized.length <= maxPoints) {
    return normalized;
  }

  return largestTriangleThreeBuckets(normalized, maxPoints);
}
