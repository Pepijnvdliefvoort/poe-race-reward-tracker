import {
    handlePriceRangeMaxChange,
    handlePriceRangeMaxLabelChange,
    handlePriceRangeMinChange,
    handlePriceRangeMinLabelChange,
    syncPriceRangeFromState,
} from "./priceRange.js";
import { dom, saveFilters, state } from "../core/state.js";
import { applyFiltersAndRender } from "./renderer.js";

const CUSTOM_AMOUNT_MAX = { day: 730, week: 104, month: 24 };

/**
 * Manages filter UI controls and event listeners.
 */

function syncChartTimespanCustomVisibility() {
    const wrap = dom.chartTimespanCustomWrap;
    const preset = state.filters.chartTimespanPreset;
    if (!wrap) {
        return;
    }
    if (preset === "custom") {
        wrap.removeAttribute("hidden");
    } else {
        wrap.setAttribute("hidden", "");
    }
}

function clampChartCustomAmount(rawAmount, unit) {
    const u = unit === "day" || unit === "week" || unit === "month" ? unit : "month";
    const max = CUSTOM_AMOUNT_MAX[u];
    const n = Math.round(Number(rawAmount));
    if (!Number.isFinite(n)) {
        return 3;
    }
    return Math.min(max, Math.max(1, n));
}

function applyChartCustomFromInputs() {
    const unit = dom.chartTimespanUnitSelect?.value || state.filters.chartTimespanCustomUnit;
    const validUnit = unit === "day" || unit === "week" || unit === "month" ? unit : "month";
    state.filters.chartTimespanCustomUnit = validUnit;
    state.filters.chartTimespanCustomAmount = clampChartCustomAmount(dom.chartTimespanAmountInput?.value, validUnit);
    if (dom.chartTimespanAmountInput) {
        dom.chartTimespanAmountInput.value = String(state.filters.chartTimespanCustomAmount);
    }
    if (dom.chartTimespanUnitSelect) {
        dom.chartTimespanUnitSelect.value = state.filters.chartTimespanCustomUnit;
    }
}

/**
 * Update the search clear button visibility based on input value.
 */
export function syncSearchClearButton() {
    if (!dom.searchClearBtn) {
        return;
    }

    const hasValue = Boolean(dom.searchInput.value.trim());
    dom.searchClearBtn.disabled = !hasValue;
}

/**
 * Sync all filter controls from current state.
 */
export function syncFilterControlsFromState() {
    dom.searchInput.value = state.filters.search;
    dom.priceSortSelect.value = state.filters.priceSort;
    dom.trendSortSelect.value = state.filters.trendSort;
    if (dom.soldSortSelect) {
        dom.soldSortSelect.value = state.filters.soldSort;
    }
    dom.favoritesOnlyInput.checked = state.filters.favoritesOnly;
    syncPriceRangeFromState();

    if (dom.chartTimespanPresetSelect) {
        dom.chartTimespanPresetSelect.value = state.filters.chartTimespanPreset;
    }
    if (dom.chartTimespanAmountInput) {
        dom.chartTimespanAmountInput.value = String(state.filters.chartTimespanCustomAmount);
    }
    if (dom.chartTimespanUnitSelect) {
        dom.chartTimespanUnitSelect.value = state.filters.chartTimespanCustomUnit;
    }
    syncChartTimespanCustomVisibility();
}

/**
 * Register all filter UI event listeners.
 */
export function registerFilterEventListeners() {
    // Search input
    dom.searchInput.addEventListener("input", (e) => {
        state.filters.search = e.target.value.toLowerCase();
        saveFilters();
        syncSearchClearButton();
        applyFiltersAndRender();
    });

    // On mobile keyboards, Enter/Done should finish editing and return to page zoom.
    dom.searchInput.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") {
            return;
        }

        event.preventDefault();
        dom.searchInput.blur();
    });

    dom.searchInput.addEventListener("blur", () => {
        forceMobileZoomOut();
    });

    // Search clear button
    dom.searchClearBtn?.addEventListener("click", () => {
        dom.searchInput.value = "";
        state.filters.search = "";
        saveFilters();
        syncSearchClearButton();
        applyFiltersAndRender();
        dom.searchInput.focus();
    });

    // Price sort
    dom.priceSortSelect.addEventListener("change", (e) => {
        state.filters.priceSort = e.target.value;
        saveFilters();
        applyFiltersAndRender();
    });

    // Trend sort
    dom.trendSortSelect.addEventListener("change", (e) => {
        state.filters.trendSort = e.target.value;
        saveFilters();
        applyFiltersAndRender();
    });

    // Est. sold (chart window)
    dom.soldSortSelect?.addEventListener("change", (e) => {
        state.filters.soldSort = e.target.value;
        saveFilters();
        applyFiltersAndRender();
    });

    // Favorites only
    dom.favoritesOnlyInput.addEventListener("change", (e) => {
        state.filters.favoritesOnly = e.target.checked;
        saveFilters();
        applyFiltersAndRender();
    });

    // Price range sliders
    dom.priceRangeMinInput.addEventListener("input", () => {
        handlePriceRangeMinChange();
        applyFiltersAndRender();
    });

    dom.priceRangeMaxInput.addEventListener("input", () => {
        handlePriceRangeMaxChange();
        applyFiltersAndRender();
    });

    // Price range manual input fields
    dom.priceRangeMinLabel.addEventListener("change", () => {
        handlePriceRangeMinLabelChange();
        applyFiltersAndRender();
    });

    dom.priceRangeMaxLabel.addEventListener("change", () => {
        handlePriceRangeMaxLabelChange();
        applyFiltersAndRender();
    });

    if (dom.chartTimespanPresetSelect) {
        dom.chartTimespanPresetSelect.addEventListener("change", (e) => {
            state.filters.chartTimespanPreset = e.target.value;
            applyChartCustomFromInputs();
            saveFilters();
            syncChartTimespanCustomVisibility();
            applyFiltersAndRender();
        });
    }

    const commitCustomIfActive = () => {
        applyChartCustomFromInputs();
        saveFilters();
        if (state.filters.chartTimespanPreset === "custom") {
            applyFiltersAndRender();
        }
    };

    if (dom.chartTimespanAmountInput) {
        dom.chartTimespanAmountInput.addEventListener("change", commitCustomIfActive);
        dom.chartTimespanAmountInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                commitCustomIfActive();
                dom.chartTimespanAmountInput.blur();
            }
        });
    }

    if (dom.chartTimespanUnitSelect) {
        dom.chartTimespanUnitSelect.addEventListener("change", () => {
            applyChartCustomFromInputs();
            saveFilters();
            if (state.filters.chartTimespanPreset === "custom") {
                applyFiltersAndRender();
            }
        });
    }

    // Reset filters button
    dom.resetFiltersBtn.addEventListener("click", () => {
        state.filters.search = "";
        state.filters.priceSort = "";
        state.filters.trendSort = "";
        state.filters.soldSort = "";
        state.filters.favoritesOnly = false;
        state.filters.priceMin = 0;
        state.filters.priceMax = 100;
        state.filters.chartTimespanPreset = "all";
        state.filters.chartTimespanCustomAmount = 3;
        state.filters.chartTimespanCustomUnit = "month";

        syncFilterControlsFromState();
        syncSearchClearButton();
        saveFilters();
        applyFiltersAndRender();
    });

    syncSearchClearButton();
}

function forceMobileZoomOut() {
    const viewport = window.visualViewport;
    if (!viewport || viewport.scale <= 1) {
        return;
    }

    const viewportMeta = document.querySelector('meta[name="viewport"]');
    if (!viewportMeta) {
        return;
    }

    const original = viewportMeta.getAttribute("content") || "width=device-width, initial-scale=1.0";
    viewportMeta.setAttribute("content", "width=device-width, initial-scale=1.0, maximum-scale=1.0");

    window.setTimeout(() => {
        viewportMeta.setAttribute("content", original);
    }, 120);
}

/**
 * Register keyboard shortcuts.
 */
export function registerKeyboardShortcuts() {
    document.addEventListener("keydown", (event) => {
        const isFindShortcut = event.ctrlKey && event.key.toLowerCase() === "f";
        if (!isFindShortcut) {
            return;
        }

        event.preventDefault();
        dom.searchInput.focus();
        dom.searchInput.select();
    });
}
