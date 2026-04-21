import { applySorting } from "../domain/sorting.js";
import { getAvailableLowestPrice } from "../domain/pricing.js";
import { state } from "../core/state.js";

/**
 * Filter and sort items based on current filter state.
 * Handles search, favorites, price range, and sorting.
 */
export function getFilteredAndSortedItems(items) {
    let filtered = [...items];

    // Apply search filter
    if (state.filters.search) {
        filtered = filtered.filter((item) => item.itemName.toLowerCase().includes(state.filters.search));
    }

    // Apply favorites filter
    if (state.filters.favoritesOnly) {
        filtered = filtered.filter((item) => state.favoriteItems.has(item.itemName));
    }

    // Apply price range filter
    if (isPriceRangeActive()) {
        filtered = filtered.filter((item) => {
            const price = getAvailableLowestPrice(item);
            if (price == null) {
                return false;
            }
            const belowCap =
                state.filters.priceMax >= state.globalPriceRange.max ||
                price <= state.filters.priceMax;
            return price >= state.filters.priceMin && belowCap;
        });
    }

    // Apply sorting
    filtered = applySorting(filtered, state.filters);

    // Reorder favorites to top
    filtered = reorderFavoritesFirst(filtered);

    return filtered;
}

/**
 * Check if any price range filters are active (non-default values).
 */
export function isPriceRangeActive() {
    return (
        state.filters.priceMin > state.globalPriceRange.min ||
        state.filters.priceMax < state.globalPriceRange.max
    );
}

/**
 * Reorder items to place favorites first, maintaining original order otherwise.
 */
export function reorderFavoritesFirst(items) {
    // If manual sorting is active, don't reorder
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

/**
 * Check if any manual sort is currently applied.
 */
export function isManualSortActive() {
    return Boolean(state.filters.priceSort || state.filters.trendSort || state.filters.soldSort);
}

/**
 * Check if any filters are currently active.
 */
export function hasActiveFilters() {
    return (
        state.filters.search ||
        state.filters.priceSort ||
        state.filters.trendSort ||
        state.filters.soldSort ||
        state.filters.favoritesOnly ||
        isPriceRangeActive()
    );
}
