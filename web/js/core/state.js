export const REFRESH_MS = 4000;
// Charts render 10 historical points + 1 forecast point.
export const MAX_POINTS = 11;
export const MAX_ACTUAL_POINTS = 10;
export const PREDICTION_POINTS = 1;
export const THREE_MONTHS_MS = 90 * 24 * 60 * 60 * 1000;
const MS_PER_DAY = 24 * 60 * 60 * 1000;

/** Preset keys for line-chart history window (default: 3 months). */
export const CHART_TIMESPAN_PRESET_MS = {
  "1w": 7 * MS_PER_DAY,
  "1m": 30 * MS_PER_DAY,
  "3m": 90 * MS_PER_DAY,
  "6m": 180 * MS_PER_DAY,
};

export const FAVORITES_STORAGE_KEY = "poe-market-favorites";
export const FILTERS_STORAGE_KEY = "poe-market-filters";

const DEFAULT_FILTERS = {
  search: "",
  priceSort: "",
  trendSort: "",
  favoritesOnly: false,
  priceMin: 0,
  priceMax: 100,
  chartTimespanPreset: "3m",
  /** Used when preset is `custom` (≈ 3 calendar months at 30d/month). */
  chartTimespanCustomAmount: 3,
  chartTimespanCustomUnit: "month",
};

const VALID_CHART_PRESETS = new Set(["1w", "1m", "3m", "6m", "all", "custom"]);
const VALID_CHART_CUSTOM_UNITS = new Set(["day", "week", "month"]);
const CUSTOM_AMOUNT_MAX = { day: 730, week: 104, month: 24 };
const CUSTOM_SPAN_CAP_MS = 730 * MS_PER_DAY;

export const dom = {
  cardsEl: document.getElementById("cards"),
  overviewEl: document.getElementById("overview"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  searchInput: document.getElementById("searchInput"),
  searchClearBtn: document.getElementById("searchClearBtn"),
  priceSortSelect: document.getElementById("priceSort"),
  trendSortSelect: document.getElementById("trendSort"),
  favoritesOnlyInput: document.getElementById("favoritesOnly"),
  priceRangeMinInput: document.getElementById("priceRangeMin"),
  priceRangeMaxInput: document.getElementById("priceRangeMax"),
  priceRangeMinLabel: document.getElementById("priceRangeMinLabel"),
  priceRangeMaxLabel: document.getElementById("priceRangeMaxLabel"),
  rangeAtCapEl: document.getElementById("rangeAtCap"),
  rangeFillEl: document.getElementById("rangeFill"),
  resetFiltersBtn: document.getElementById("resetFiltersBtn"),
  chartTimespanPresetSelect: document.getElementById("chartTimespanPreset"),
  chartTimespanCustomWrap: document.getElementById("chartTimespanCustomWrap"),
  chartTimespanAmountInput: document.getElementById("chartTimespanAmount"),
  chartTimespanUnitSelect: document.getElementById("chartTimespanUnit"),
};

export const chartMap = new Map();

export const state = {
  filters: loadFilters(),
  globalPriceRange: { min: 0, max: 100 },
  currentItems: [],
  nextInLineItemName: null,
  favoriteItems: loadFavorites(),
};

/**
 * Milliseconds of history to include in line charts (and trend sort), from filter state.
 * Returns `Infinity` for the "all time" preset (no lower time bound).
 */
export function getChartTimespanMs() {
  const f = state.filters;
  if (f.chartTimespanPreset === "all") {
    return Infinity;
  }
  if (f.chartTimespanPreset === "custom") {
    const unit = VALID_CHART_CUSTOM_UNITS.has(f.chartTimespanCustomUnit) ? f.chartTimespanCustomUnit : "month";
    const maxAmt = CUSTOM_AMOUNT_MAX[unit];
    let amt = Math.round(Number(f.chartTimespanCustomAmount));
    if (!Number.isFinite(amt)) {
      amt = DEFAULT_FILTERS.chartTimespanCustomAmount;
    }
    amt = Math.min(maxAmt, Math.max(1, amt));
    let spanMs;
    if (unit === "day") {
      spanMs = amt * MS_PER_DAY;
    } else if (unit === "week") {
      spanMs = amt * 7 * MS_PER_DAY;
    } else {
      spanMs = amt * 30 * MS_PER_DAY;
    }
    return Math.min(CUSTOM_SPAN_CAP_MS, spanMs);
  }
  return CHART_TIMESPAN_PRESET_MS[f.chartTimespanPreset] ?? CHART_TIMESPAN_PRESET_MS["3m"];
}

function parseNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function loadFilters() {
  try {
    const raw = localStorage.getItem(FILTERS_STORAGE_KEY);
    if (!raw) {
      return { ...DEFAULT_FILTERS };
    }

    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      return { ...DEFAULT_FILTERS };
    }

    const priceMin = parseNumber(parsed.priceMin, DEFAULT_FILTERS.priceMin);
    const priceMax = parseNumber(parsed.priceMax, DEFAULT_FILTERS.priceMax);
    const normalizedMin = Math.min(priceMin, priceMax);
    const normalizedMax = Math.max(priceMin, priceMax);

    const presetRaw = parsed.chartTimespanPreset === "1y" ? "all" : parsed.chartTimespanPreset;
    const chartTimespanPreset =
      typeof presetRaw === "string" && VALID_CHART_PRESETS.has(presetRaw) ? presetRaw : DEFAULT_FILTERS.chartTimespanPreset;

    const unitRaw = parsed.chartTimespanCustomUnit;
    let chartTimespanCustomUnit =
      typeof unitRaw === "string" && VALID_CHART_CUSTOM_UNITS.has(unitRaw) ? unitRaw : DEFAULT_FILTERS.chartTimespanCustomUnit;

    let chartTimespanCustomAmount = parseNumber(parsed.chartTimespanCustomAmount, NaN);
    if (!Number.isFinite(chartTimespanCustomAmount) && parsed.chartTimespanCustomDays != null) {
      chartTimespanCustomAmount = parseNumber(parsed.chartTimespanCustomDays, DEFAULT_FILTERS.chartTimespanCustomAmount);
      chartTimespanCustomUnit = "day";
    }
    if (!Number.isFinite(chartTimespanCustomAmount)) {
      chartTimespanCustomAmount = DEFAULT_FILTERS.chartTimespanCustomAmount;
    }
    const maxAmt = CUSTOM_AMOUNT_MAX[chartTimespanCustomUnit] ?? CUSTOM_AMOUNT_MAX.month;
    chartTimespanCustomAmount = Math.min(maxAmt, Math.max(1, Math.round(chartTimespanCustomAmount)));

    return {
      search: typeof parsed.search === "string" ? parsed.search.toLowerCase() : DEFAULT_FILTERS.search,
      priceSort: parsed.priceSort === "asc" || parsed.priceSort === "desc" ? parsed.priceSort : "",
      trendSort:
        parsed.trendSort === "highest" || parsed.trendSort === "lowest" ? parsed.trendSort : "",
      favoritesOnly: Boolean(parsed.favoritesOnly),
      priceMin: normalizedMin,
      priceMax: normalizedMax,
      chartTimespanPreset,
      chartTimespanCustomAmount,
      chartTimespanCustomUnit,
    };
  } catch {
    return { ...DEFAULT_FILTERS };
  }
}

export function saveFilters() {
  localStorage.setItem(
    FILTERS_STORAGE_KEY,
    JSON.stringify({
      search: state.filters.search,
      priceSort: state.filters.priceSort,
      trendSort: state.filters.trendSort,
      favoritesOnly: state.filters.favoritesOnly,
      priceMin: state.filters.priceMin,
      priceMax: state.filters.priceMax,
      chartTimespanPreset: state.filters.chartTimespanPreset,
      chartTimespanCustomAmount: state.filters.chartTimespanCustomAmount,
      chartTimespanCustomUnit: state.filters.chartTimespanCustomUnit,
    })
  );
}

export function loadFavorites() {
  try {
    const raw = localStorage.getItem(FAVORITES_STORAGE_KEY);
    if (!raw) {
      return new Set();
    }

    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return new Set();
    }

    return new Set(parsed.filter((value) => typeof value === "string"));
  } catch {
    return new Set();
  }
}

export function saveFavorites() {
  localStorage.setItem(FAVORITES_STORAGE_KEY, JSON.stringify([...state.favoriteItems]));
}
