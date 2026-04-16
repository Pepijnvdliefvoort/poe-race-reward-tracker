import { applySorting, getAvailableLowestPrice, updateAllCards, showNoFilterResults } from "./js/cards.js";
import { dom, REFRESH_MS, saveFilters, state } from "./js/state.js";
import { formatTime } from "./js/utils.js";
import { initTheme, toggleTheme } from "./js/theme.js";

function isPriceRangeActive() {
  return (
    state.filters.priceMin > state.globalPriceRange.min ||
    state.filters.priceMax < state.globalPriceRange.max
  );
}

function updateRangeFill() {
  const { min, max } = state.globalPriceRange;
  const range = max - min || 1;
  const leftPct = ((state.filters.priceMin - min) / range) * 100;
  const rightPct = ((state.filters.priceMax - min) / range) * 100;
  dom.rangeFillEl.style.left = `${leftPct}%`;
  dom.rangeFillEl.style.width = `${rightPct - leftPct}%`;
}

function setMinLabel(value) {
  dom.priceRangeMinLabel.value = Math.round(value);
}

function setMaxLabel(value) {
  dom.priceRangeMaxLabel.value = Math.round(value);
  dom.rangeAtCapEl.classList.toggle("visible", value >= state.globalPriceRange.max);
}

function initPriceRangeSlider() {
  const newMin = 0;
  const newMax = 100;

  if (state.globalPriceRange.min === newMin && state.globalPriceRange.max === newMax) {
    setMinLabel(state.filters.priceMin);
    setMaxLabel(state.filters.priceMax);
    updateRangeFill();
    return;
  }

  state.globalPriceRange = { min: newMin, max: newMax };
  const clampedMin = Math.max(newMin, Math.min(state.filters.priceMin, newMax));
  const clampedMax = Math.max(clampedMin, Math.min(state.filters.priceMax, newMax));
  state.filters.priceMin = clampedMin;
  state.filters.priceMax = clampedMax;

  for (const input of [dom.priceRangeMinInput, dom.priceRangeMaxInput]) {
    input.min = newMin;
    input.max = newMax;
    input.step = 1;
    input.value = input === dom.priceRangeMinInput ? clampedMin : clampedMax;
  }

  dom.priceRangeMinLabel.min = newMin;
  dom.priceRangeMinLabel.max = newMax;
  dom.priceRangeMaxLabel.min = newMin;
  dom.priceRangeMaxLabel.max = newMax;

  setMinLabel(clampedMin);
  setMaxLabel(clampedMax);
  updateRangeFill();
  saveFilters();
}

function isManualSortActive() {
  return Boolean(state.filters.priceSort || state.filters.trendSort);
}

function reorderFavoritesFirst(items) {
  if (isManualSortActive()) {
    return items;
  }

  const withIndex = items.map((item, index) => ({ item, index }));
  withIndex.sort((a, b) => {
    const aFav = state.favoriteItems.has(a.item.itemName) ? 0 : 1;
    const bFav = state.favoriteItems.has(b.item.itemName) ? 0 : 1;
    if (aFav !== bFav) {
      return aFav - bFav;
    }
    return a.index - b.index;
  });

  return withIndex.map((entry) => entry.item);
}

function refreshVisibleOrdering() {
  const hasActiveFilters =
    state.filters.search ||
    state.filters.priceSort ||
    state.filters.trendSort ||
    state.filters.favoritesOnly ||
    isPriceRangeActive();

  if (hasActiveFilters) {
    applyFiltersAndSort();
  } else {
    updateAllCards(reorderFavoritesFirst(state.currentItems), refreshVisibleOrdering);
  }
}

function applyFiltersAndSort() {
  let filtered = [...state.currentItems];

  dom.cardsEl.querySelector(".empty")?.remove();

  if (state.filters.search) {
    filtered = filtered.filter((item) => item.itemName.toLowerCase().includes(state.filters.search));
  }

  if (state.filters.favoritesOnly) {
    filtered = filtered.filter((item) => state.favoriteItems.has(item.itemName));
  }

  if (isPriceRangeActive()) {
    filtered = filtered.filter((item) => {
      const price = getAvailableLowestPrice(item);
      if (price == null) {
        return false;
      }
      const belowCap = state.filters.priceMax >= state.globalPriceRange.max || price <= state.filters.priceMax;
      return price >= state.filters.priceMin && belowCap;
    });
  }

  filtered = applySorting(filtered, state.filters);
  filtered = reorderFavoritesFirst(filtered);

  updateAllCards(filtered, refreshVisibleOrdering);

  if (filtered.length === 0) {
    showNoFilterResults();
  }
}

function syncSearchClearButton() {
  if (!dom.searchClearBtn) {
    return;
  }

  const hasValue = Boolean(dom.searchInput.value.trim());
  dom.searchClearBtn.disabled = !hasValue;
}

function syncFilterControlsFromState() {
  dom.searchInput.value = state.filters.search;
  dom.priceSortSelect.value = state.filters.priceSort;
  dom.trendSortSelect.value = state.filters.trendSort;
  dom.favoritesOnlyInput.checked = state.filters.favoritesOnly;
  dom.priceRangeMinInput.value = state.filters.priceMin;
  dom.priceRangeMaxInput.value = state.filters.priceMax;
  setMinLabel(state.filters.priceMin);
  setMaxLabel(state.filters.priceMax);
  updateRangeFill();
}

function getNextInLineItemName(items) {
  if (!items.length) {
    return null;
  }

  let latestItem = null;
  let latestTime = -Infinity;

  // Find the item that was polled most recently (by timestamp, not cycle)
  for (let i = 0; i < items.length; i += 1) {
    const latest = items[i].latest;
    if (!latest) {
      continue;
    }

    const time = latest.time ?? -Infinity;

    if (time > latestTime) {
      latestTime = time;
      latestItem = items[i];
    }
  }

  // If no item has been polled yet, start with the first one (lowest sortOrder)
  if (latestItem === null) {
    const first = items.reduce((min, it) => 
      (it.sortOrder ?? Infinity) < (min.sortOrder ?? Infinity) ? it : min
    );
    return first?.itemName ?? null;
  }

  // Find the next item in sortOrder sequence
  const latestOrder = latestItem.sortOrder ?? Infinity;
  let nextItem = null;
  let nextOrder = Infinity;

  for (let i = 0; i < items.length; i += 1) {
    const order = items[i].sortOrder ?? Infinity;
    // Find smallest order > latestOrder (next in cycle)
    if (order > latestOrder && order < nextOrder) {
      nextOrder = order;
      nextItem = items[i];
    }
  }

  // If no next item found (we're at the end), wrap to the beginning
  if (nextItem === null) {
    nextItem = items.reduce((min, it) => 
      (it.sortOrder ?? Infinity) < (min.sortOrder ?? Infinity) ? it : min
    );
  }

  return nextItem?.itemName ?? null;
}

function setStatus(stateName, text) {
  dom.statusDot.classList.remove("ok", "warn", "error");
  dom.statusDot.classList.add(stateName);
  dom.statusText.textContent = text;
}

function statTile(label, value) {
  const tile = document.createElement("article");
  tile.className = "stat-tile";
  tile.innerHTML = `
    <div class="stat-label">${label}</div>
    <div class="stat-value">${value}</div>
  `;
  return tile;
}

function updateOverview(payload) {
  const items = payload.items || [];
  const totalRows = payload.rowCount || 0;
  const nextPollTime = payload.nextPollTime;

  const tiles = [statTile("Tracked Items", String(items.length)), statTile("Data points", String(totalRows))];

  if (nextPollTime) {
    const nextPollDate = new Date(nextPollTime);
    const timeString = nextPollDate.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
    tiles.push(statTile("Next Poll", timeString));
  }

  dom.overviewEl.replaceChildren(...tiles);
}

function render(payload) {
  updateOverview(payload);
  state.currentItems = payload.items || [];

  if (!state.currentItems.length) {
    dom.cardsEl.innerHTML = '<div class="empty">No item rows yet. Start your poller and wait for CSV updates.</div>';
    return;
  }

  // Recalculate next-in-line from FRESH data before any sorting/filtering
  const nextName = getNextInLineItemName(state.currentItems);
  state.nextInLineItemName = nextName;

  initPriceRangeSlider();

  const hasActiveFilters =
    state.filters.search ||
    state.filters.priceSort ||
    state.filters.trendSort ||
    state.filters.favoritesOnly ||
    isPriceRangeActive();

  if (hasActiveFilters) {
    applyFiltersAndSort();
  } else {
    updateAllCards(reorderFavoritesFirst(state.currentItems), refreshVisibleOrdering);
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/prices", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const payload = await response.json();
    render(payload);
    const latestTs = (payload.items || [])
      .flatMap((item) => item.points || [])
      .map((point) => point.time)
      .filter(Boolean)
      .reduce((max, t) => Math.max(max, t), 0);
    const timeLabel = latestTs ? formatTime(latestTs, true) : formatTime(Date.now(), true);
    const ageMs = latestTs ? Date.now() - latestTs : 0;
    const stale = latestTs && ageMs > 60 * 30 * 1000;
    setStatus(stale ? "warn" : "ok", stale ? `Stale ${timeLabel}` : `Live ${timeLabel}`);
  } catch (error) {
    setStatus("error", `Error: ${error.message}`);
  }
}

function registerEventListeners() {
  document.addEventListener("keydown", (event) => {
    const isFindShortcut = event.ctrlKey && event.key.toLowerCase() === "f";
    if (!isFindShortcut) {
      return;
    }

    event.preventDefault();
    dom.searchInput.focus();
    dom.searchInput.select();
  });

  syncFilterControlsFromState();

  dom.searchInput.addEventListener("input", (e) => {
    state.filters.search = e.target.value.toLowerCase();
    saveFilters();
    syncSearchClearButton();
    applyFiltersAndSort();
  });

  dom.searchClearBtn?.addEventListener("click", () => {
    dom.searchInput.value = "";
    state.filters.search = "";
    saveFilters();
    syncSearchClearButton();
    applyFiltersAndSort();
    dom.searchInput.focus();
  });

  const themeToggle = document.getElementById("themeToggle");
  if (themeToggle) {
    themeToggle.addEventListener("click", toggleTheme);
  }

  dom.priceSortSelect.addEventListener("change", (e) => {
    state.filters.priceSort = e.target.value;
    saveFilters();
    applyFiltersAndSort();
  });

  dom.trendSortSelect.addEventListener("change", (e) => {
    state.filters.trendSort = e.target.value;
    saveFilters();
    applyFiltersAndSort();
  });

  dom.favoritesOnlyInput.addEventListener("change", (e) => {
    state.filters.favoritesOnly = e.target.checked;
    saveFilters();
    applyFiltersAndSort();
  });

  dom.resetFiltersBtn.addEventListener("click", () => {
    state.filters.search = "";
    state.filters.priceSort = "";
    state.filters.trendSort = "";
    state.filters.favoritesOnly = false;
    state.filters.priceMin = 0;
    state.filters.priceMax = 100;

    dom.searchInput.value = "";
    syncSearchClearButton();
    dom.priceSortSelect.value = "";
    dom.trendSortSelect.value = "";
    dom.favoritesOnlyInput.checked = false;
    dom.priceRangeMinInput.value = 0;
    dom.priceRangeMaxInput.value = 100;
    setMinLabel(0);
    setMaxLabel(100);
    updateRangeFill();

    saveFilters();
    applyFiltersAndSort();
  });

  dom.priceRangeMinInput.addEventListener("input", () => {
    dom.priceRangeMinInput.style.zIndex = 2;
    dom.priceRangeMaxInput.style.zIndex = 1;
    const val = parseFloat(dom.priceRangeMinInput.value);
    if (val > state.filters.priceMax) {
      dom.priceRangeMinInput.value = state.filters.priceMax;
      state.filters.priceMin = state.filters.priceMax;
    } else {
      state.filters.priceMin = val;
    }
    setMinLabel(state.filters.priceMin);
    updateRangeFill();
    saveFilters();
    applyFiltersAndSort();
  });

  dom.priceRangeMaxInput.addEventListener("input", () => {
    dom.priceRangeMaxInput.style.zIndex = 2;
    dom.priceRangeMinInput.style.zIndex = 1;
    const val = parseFloat(dom.priceRangeMaxInput.value);
    if (val < state.filters.priceMin) {
      dom.priceRangeMaxInput.value = state.filters.priceMin;
      state.filters.priceMax = state.filters.priceMin;
    } else {
      state.filters.priceMax = val;
    }
    setMaxLabel(state.filters.priceMax);
    updateRangeFill();
    saveFilters();
    applyFiltersAndSort();
  });

  dom.priceRangeMinLabel.addEventListener("change", () => {
    let val = parseInt(dom.priceRangeMinLabel.value, 10);
    if (Number.isNaN(val)) val = state.globalPriceRange.min;
    val = Math.max(state.globalPriceRange.min, Math.min(val, state.filters.priceMax));
    state.filters.priceMin = val;
    dom.priceRangeMinInput.value = val;
    setMinLabel(val);
    updateRangeFill();
    saveFilters();
    applyFiltersAndSort();
  });

  dom.priceRangeMaxLabel.addEventListener("change", () => {
    let val = parseInt(dom.priceRangeMaxLabel.value, 10);
    if (Number.isNaN(val)) val = state.globalPriceRange.max;
    val = Math.max(state.filters.priceMin, Math.min(val, state.globalPriceRange.max));
    state.filters.priceMax = val;
    dom.priceRangeMaxInput.value = val;
    setMaxLabel(val);
    updateRangeFill();
    saveFilters();
    applyFiltersAndSort();
  });

  syncSearchClearButton();
}

registerEventListeners();
initTheme();
refresh();
setInterval(refresh, REFRESH_MS);
