import {
    handlePriceRangeMaxChange,
    handlePriceRangeMaxLabelChange,
    handlePriceRangeMinChange,
    handlePriceRangeMinLabelChange,
    syncPriceRangeFromState,
} from "./priceRange.js";
import { isPriceRangeActive } from "./filters.js";
import { dom, saveFilters, state } from "../core/state.js";
import { applyFiltersAndRender, refresh } from "./renderer.js";

const CUSTOM_AMOUNT_MAX = { day: 730, week: 104, month: 24 };

/**
 * The dashboard toolbar: search, one "Sort" menu, the chart period, favorites, a price
 * popover and a Reset button that only shows when something narrows the list.
 *
 * Sorting still uses the three stored keys (priceSort / trendSort / soldSort) so saved
 * filters and sorting.js keep working; the menu sets exactly one of them.
 */
const SORT_OPTIONS = {
    "": { priceSort: "", trendSort: "", soldSort: "" },
    "price-desc": { priceSort: "desc", trendSort: "", soldSort: "" },
    "price-asc": { priceSort: "asc", trendSort: "", soldSort: "" },
    "trend-high": { priceSort: "", trendSort: "highest", soldSort: "" },
    "trend-low": { priceSort: "", trendSort: "lowest", soldSort: "" },
    "sold-high": { priceSort: "", trendSort: "", soldSort: "high" },
    "sold-low": { priceSort: "", trendSort: "", soldSort: "low" },
};

/** The menu value for the stored sort keys (the last-applied sort wins, as in sorting.js). */
function sortValueFromState() {
    const f = state.filters;
    if (f.soldSort) return f.soldSort === "high" ? "sold-high" : "sold-low";
    if (f.trendSort) return f.trendSort === "highest" ? "trend-high" : "trend-low";
    if (f.priceSort) return f.priceSort === "desc" ? "price-desc" : "price-asc";
    return "";
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

function syncPeriodControls() {
    const preset = state.filters.chartTimespanPreset;
    for (const btn of dom.periodButtons) {
        btn.setAttribute("aria-checked", btn.dataset.period === preset ? "true" : "false");
        btn.tabIndex = btn.dataset.period === preset ? 0 : -1;
    }
    if (dom.chartTimespanCustomWrap) {
        dom.chartTimespanCustomWrap.hidden = preset !== "custom";
    }
}

function formatMirrors(value) {
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** Price chip shows the active range; Reset only shows when the list is narrowed or sorted. */
export function syncToolbarState() {
    const priceActive = isPriceRangeActive();
    if (dom.priceFilterBtn) {
        dom.priceFilterBtn.classList.toggle("is-active", priceActive);
        const atCap = state.filters.priceMax >= state.globalPriceRange.max;
        dom.priceFilterValue.textContent = priceActive
            ? `${formatMirrors(state.filters.priceMin)}–${formatMirrors(state.filters.priceMax)}${atCap ? "+" : ""}`
            : "";
    }
    if (dom.favoritesOnlyBtn) {
        dom.favoritesOnlyBtn.setAttribute("aria-pressed", state.filters.favoritesOnly ? "true" : "false");
    }
    if (dom.resetFiltersBtn) {
        const f = state.filters;
        dom.resetFiltersBtn.hidden = !(f.search.trim() || sortValueFromState() || f.favoritesOnly || priceActive);
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
    if (dom.sortBySelect) {
        const value = sortValueFromState();
        dom.sortBySelect.value = value;
        // Older saved filters could combine sorts; keep only the one the menu shows.
        Object.assign(state.filters, SORT_OPTIONS[value]);
    }
    syncPriceRangeFromState();

    if (dom.chartTimespanAmountInput) {
        dom.chartTimespanAmountInput.value = String(state.filters.chartTimespanCustomAmount);
    }
    if (dom.chartTimespanUnitSelect) {
        dom.chartTimespanUnitSelect.value = state.filters.chartTimespanCustomUnit;
    }
    syncPeriodControls();
    syncToolbarState();
}

function setPricePopover(open) {
    if (!dom.pricePopover || !dom.priceFilterBtn) return;
    dom.pricePopover.hidden = !open;
    dom.priceFilterBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
        window.requestAnimationFrame(() => dom.priceRangeMinLabel?.focus({ preventScroll: true }));
    }
}

/**
 * Register all filter UI event listeners.
 */
export function registerFilterEventListeners() {
    const rerender = () => {
        applyFiltersAndRender();
        syncToolbarState();
    };

    // Search input
    dom.searchInput.addEventListener("input", (e) => {
        state.filters.search = e.target.value;
        saveFilters();
        syncSearchClearButton();
        rerender();
    });

    // On mobile keyboards, Enter/Done should finish editing and return to page zoom.
    dom.searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && dom.searchInput.value) {
            event.preventDefault();
            dom.searchClearBtn?.click();
            return;
        }
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
        rerender();
        dom.searchInput.focus();
    });

    // Sort (one menu for price / trend / est. sold)
    dom.sortBySelect?.addEventListener("change", (e) => {
        Object.assign(state.filters, SORT_OPTIONS[e.target.value] || SORT_OPTIONS[""]);
        saveFilters();
        rerender();
    });

    // Favorites toggle
    dom.favoritesOnlyBtn?.addEventListener("click", () => {
        state.filters.favoritesOnly = !state.filters.favoritesOnly;
        saveFilters();
        rerender();
    });

    // Chart period (segmented control; arrow keys move between options like a radio group)
    const choosePeriod = (period) => {
        if (state.filters.chartTimespanPreset === period) return;
        state.filters.chartTimespanPreset = period;
        applyChartCustomFromInputs();
        saveFilters();
        syncPeriodControls();
        rerender();
        void refresh();
        if (period === "custom") dom.chartTimespanAmountInput?.focus();
    };
    dom.periodButtons.forEach((btn, i) => {
        btn.addEventListener("click", () => choosePeriod(btn.dataset.period));
        btn.addEventListener("keydown", (event) => {
            if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
            event.preventDefault();
            const next = dom.periodButtons[(i + (event.key === "ArrowRight" ? 1 : -1) + dom.periodButtons.length) % dom.periodButtons.length];
            next.focus();
            choosePeriod(next.dataset.period);
        });
    });

    const commitCustomIfActive = () => {
        applyChartCustomFromInputs();
        saveFilters();
        if (state.filters.chartTimespanPreset === "custom") {
            rerender();
            void refresh();
        }
    };
    dom.chartTimespanAmountInput?.addEventListener("change", commitCustomIfActive);
    dom.chartTimespanAmountInput?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            commitCustomIfActive();
            dom.chartTimespanAmountInput.blur();
        }
    });
    dom.chartTimespanUnitSelect?.addEventListener("change", commitCustomIfActive);

    // Price popover
    dom.priceFilterBtn?.addEventListener("click", () => setPricePopover(dom.pricePopover.hidden));
    document.addEventListener("pointerdown", (event) => {
        if (dom.pricePopover?.hidden) return;
        if (!event.target.closest?.(".dash-popover-wrap")) setPricePopover(false);
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && dom.pricePopover && !dom.pricePopover.hidden) {
            setPricePopover(false);
            dom.priceFilterBtn.focus();
        }
    });

    // Price range sliders
    dom.priceRangeMinInput.addEventListener("input", () => {
        handlePriceRangeMinChange();
        rerender();
    });

    dom.priceRangeMaxInput.addEventListener("input", () => {
        handlePriceRangeMaxChange();
        rerender();
    });

    // Price range manual input fields
    dom.priceRangeMinLabel.addEventListener("change", () => {
        handlePriceRangeMinLabelChange();
        rerender();
    });

    dom.priceRangeMaxLabel.addEventListener("change", () => {
        handlePriceRangeMaxLabelChange();
        rerender();
    });

    // Reset clears what narrows or reorders the list; the chart period is a view setting and stays.
    dom.resetFiltersBtn.addEventListener("click", () => {
        state.filters.search = "";
        Object.assign(state.filters, SORT_OPTIONS[""]);
        state.filters.favoritesOnly = false;
        state.filters.priceMin = state.globalPriceRange.min;
        state.filters.priceMax = state.globalPriceRange.max;

        syncFilterControlsFromState();
        syncSearchClearButton();
        saveFilters();
        rerender();
        setPricePopover(false);
        dom.searchInput.focus({ preventScroll: true });
    });

    // New data can change the price bounds (and so whether the price filter is active).
    document.addEventListener("dashboard:price-range", syncToolbarState);

    syncSearchClearButton();
    syncToolbarState();
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
