import { updateAllCards } from "./cards.js";
import { getFilteredAndSortedItems, hasActiveFilters } from "./filters.js";
import { initPriceRangeSlider } from "./priceRange.js";
import { dom, state } from "./state.js";
import { formatTime } from "./utils.js";

/**
 * Manages data rendering and refresh  operations.
 */

/**
 * Find the next item to be polled based on sort order and latest poll time.
 */
export function getNextInLineItemName(items) {
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

/**
 * Format milliseconds as hh:mm:ss
 */
function formatCountdown(ms) {
    if (ms <= 0) return "00:00:00";
    
    const totalSeconds = Math.floor(ms / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

/**
 * Update the overview panel with metadata (next poll time, etc).
 */
export function updateOverview(payload) {
    const nextPollTime = payload.nextPollTime;

    const pill = document.createElement("div");
    pill.className = "meta next-poll-pill";
    if (nextPollTime) {
        // Update countdown immediately
        const updateCountdown = () => {
            const now = Date.now();
            const timeRemaining = nextPollTime - now;
            const countdownString = formatCountdown(timeRemaining);
            pill.innerHTML = `⏰ Next poll: ${countdownString}`;
        };
        
        updateCountdown();
        
        // Update every second
        const interval = setInterval(updateCountdown, 1000);
        
        // Store interval ID on the pill so we can clear it later if needed
        pill.dataset.countdownInterval = interval;
    } else {
        pill.innerHTML = `⏰ Next poll: —`;
    }
    
    // Clear any existing interval from previous pill
    const oldPill = dom.overviewEl.querySelector(".next-poll-pill");
    if (oldPill?.dataset.countdownInterval) {
        clearInterval(parseInt(oldPill.dataset.countdownInterval));
    }
    
    dom.overviewEl.replaceChildren(pill);
}

/**
 * Set the status indicator (color and message).
 */
export function setStatus(stateName, text) {
    dom.statusDot.classList.remove("ok", "warn", "error");
    dom.statusDot.classList.add(stateName);
    dom.statusText.textContent = text;
}

/**
 * Render the full page with price data payload.
 */
export function render(payload) {
    updateOverview(payload);
    state.currentItems = payload.items || [];

    if (!state.currentItems.length) {
        dom.cardsEl.innerHTML =
            '<div class="empty">No item rows yet. Start your poller and wait for CSV updates.</div>';
        return;
    }

    // Recalculate next-in-line from FRESH data before any sorting/filtering
    const nextName = getNextInLineItemName(state.currentItems);
    state.nextInLineItemName = nextName;

    initPriceRangeSlider();

    if (hasActiveFilters()) {
        applyFiltersAndRender();
    } else {
        const reordered = getFilteredAndSortedItems(state.currentItems);
        updateAllCards(reordered, applyFiltersAndRender);
    }
}

/**
 * Apply current filters and re-render cards.
 */
export function applyFiltersAndRender() {
    const filtered = getFilteredAndSortedItems(state.currentItems);
    updateAllCards(filtered, applyFiltersAndRender);

    if (filtered.length === 0) {
        dom.cardsEl.innerHTML = '<div class="empty">No items match your filters.</div>';
    }
}

/**
 * Fetch price data from backend and render.
 */
export async function refresh() {
    try {
        const response = await fetch("/api/prices", { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const payload = await response.json();
        render(payload);

        // Find latest timestamp from all points for status display
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
