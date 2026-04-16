export const REFRESH_MS = 4000;
export const MAX_POINTS = 8;
export const ONE_MONTH_MS = 30 * 24 * 60 * 60 * 1000;
export const FAVORITES_STORAGE_KEY = "poe-market-favorites";

export const dom = {
  cardsEl: document.getElementById("cards"),
  overviewEl: document.getElementById("overview"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  searchInput: document.getElementById("searchInput"),
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
  filters: {
    search: "",
    priceSort: "",
    trendSort: "",
    favoritesOnly: false,
    priceMin: 0,
    priceMax: 100,
  },
  globalPriceRange: { min: 0, max: 100 },
  currentItems: [],
  nextInLineItemName: null,
  favoriteItems: loadFavorites(),
};

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
