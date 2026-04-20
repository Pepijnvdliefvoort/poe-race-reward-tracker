export const REFRESH_MS = 4000;
// Charts render 10 historical points + 1 forecast point.
export const MAX_POINTS = 11;
export const MAX_ACTUAL_POINTS = 10;
export const PREDICTION_POINTS = 1;
export const THREE_MONTHS_MS = 90 * 24 * 60 * 60 * 1000;
export const FAVORITES_STORAGE_KEY = "poe-market-favorites";
export const FILTERS_STORAGE_KEY = "poe-market-filters";

const DEFAULT_FILTERS = {
  search: "",
  priceSort: "",
  trendSort: "",
  favoritesOnly: false,
  priceMin: 0,
  priceMax: 100,
};

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
};

export const chartMap = new Map();

export const state = {
  filters: loadFilters(),
  globalPriceRange: { min: 0, max: 100 },
  currentItems: [],
  nextInLineItemName: null,
  favoriteItems: loadFavorites(),
};

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

    return {
      search: typeof parsed.search === "string" ? parsed.search.toLowerCase() : DEFAULT_FILTERS.search,
      priceSort: parsed.priceSort === "asc" || parsed.priceSort === "desc" ? parsed.priceSort : "",
      trendSort:
        parsed.trendSort === "highest" || parsed.trendSort === "lowest" ? parsed.trendSort : "",
      favoritesOnly: Boolean(parsed.favoritesOnly),
      priceMin: normalizedMin,
      priceMax: normalizedMax,
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
