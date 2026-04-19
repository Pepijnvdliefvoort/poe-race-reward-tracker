import { getAvailableLowestPrice } from "./pricing.js";
import { getTrendDirection, getTrendPercentage } from "./trends.js";

function compareByPriceWithMissingLast(a, b, direction) {
  const aPrice = getAvailableLowestPrice(a);
  const bPrice = getAvailableLowestPrice(b);
  const aMissing = aPrice == null;
  const bMissing = bPrice == null;

  if (aMissing && bMissing) {
    return 0;
  }
  if (aMissing) {
    return 1;
  }
  if (bMissing) {
    return -1;
  }

  return direction === "asc" ? aPrice - bPrice : bPrice - aPrice;
}

function compareTrendHighest(a, b) {
  // Direction priority: up > flat > down
  const directionOrder = { up: 0, flat: 1, down: 2 };
  const dirA = getTrendDirection(a);
  const dirB = getTrendDirection(b);

  if (directionOrder[dirA] !== directionOrder[dirB]) {
    return directionOrder[dirA] - directionOrder[dirB];
  }

  // Within same direction, sort by percentage
  return getTrendPercentage(b) - getTrendPercentage(a);
}

function compareTrendLowest(a, b) {
  // Direction priority: down > flat > up
  const directionOrder = { down: 0, flat: 1, up: 2 };
  const dirA = getTrendDirection(a);
  const dirB = getTrendDirection(b);

  if (directionOrder[dirA] !== directionOrder[dirB]) {
    return directionOrder[dirA] - directionOrder[dirB];
  }

  // Within same direction, sort by percentage
  return getTrendPercentage(a) - getTrendPercentage(b);
}

export function applySorting(filtered, filters) {
  if (filters.priceSort === "asc") {
    filtered.sort((a, b) => compareByPriceWithMissingLast(a, b, "asc"));
  } else if (filters.priceSort === "desc") {
    filtered.sort((a, b) => compareByPriceWithMissingLast(a, b, "desc"));
  }

  if (filters.trendSort === "highest") {
    filtered.sort((a, b) => compareTrendHighest(a, b));
  } else if (filters.trendSort === "lowest") {
    filtered.sort((a, b) => compareTrendLowest(a, b));
  }

  return filtered;
}

