import {
    handlePriceRangeMaxChange,
    handlePriceRangeMaxLabelChange,
    handlePriceRangeMinChange,
    handlePriceRangeMinLabelChange,
    syncPriceRangeFromState,
} from "./priceRange.js";
import { dom, saveFilters, state } from "../core/state.js";
import { applyFiltersAndRender } from "./renderer.js";

/**
 * Manages filter UI controls and event listeners.
 */

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
    dom.favoritesOnlyInput.checked = state.filters.favoritesOnly;
    syncPriceRangeFromState();
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

    // Reset filters button
    dom.resetFiltersBtn.addEventListener("click", () => {
        state.filters.search = "";
        state.filters.priceSort = "";
        state.filters.trendSort = "";
        state.filters.favoritesOnly = false;
        state.filters.priceMin = 0;
        state.filters.priceMax = 100;

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
