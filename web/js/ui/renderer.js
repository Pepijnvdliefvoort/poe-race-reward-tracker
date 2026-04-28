import { updateAllCards } from "./cards.js";
import { getFilteredAndSortedItems, hasActiveFilters } from "./filters.js";
import { initPriceRangeSlider } from "./priceRange.js";
import { dom, state } from "../core/state.js";
import { formatTime } from "../core/utils.js";

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
    let latestCycle = -Infinity;
    let latestOrderForTie = -Infinity;

    // Find the item that was polled most recently.
    //
    // IMPORTANT: multiple polls can share the same millisecond timestamp (or the backend can
    // round timestamps), which makes a simple `latest.time` comparison unstable and can make
    // the UI "skip" the green border. We add tie-breakers:
    // - `latest.cycle` (higher means later, when available)
    // - `sortOrder` (higher means later within a cycle when timestamps tie)
    for (let i = 0; i < items.length; i += 1) {
        const latest = items[i].latest;
        if (!latest) {
            continue;
        }

        const time = latest.time ?? -Infinity;
        const cycle = latest.cycle ?? -Infinity;
        const order = items[i].sortOrder ?? Infinity;
        const orderForTie = Number.isFinite(order) ? order : Infinity;

        if (
            time > latestTime ||
            (time === latestTime && cycle > latestCycle) ||
            (time === latestTime && cycle === latestCycle && orderForTie > latestOrderForTie)
        ) {
            latestTime = time;
            latestCycle = cycle;
            latestOrderForTie = orderForTie;
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
    const icon = document.createElement("span");
    icon.className = "meta-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "⏰";

    const label = document.createElement("span");
    label.className = "meta-label";
    label.textContent = "Next poll";

    const value = document.createElement("span");
    value.className = "meta-value meta-value--mono";

    pill.appendChild(icon);
    pill.appendChild(label);
    pill.appendChild(value);

    if (nextPollTime) {
        const updateCountdown = () => {
            const now = Date.now();
            const timeRemaining = nextPollTime - now;
            value.textContent = formatCountdown(timeRemaining);
        };

        updateCountdown();

        const interval = setInterval(updateCountdown, 1000);
        pill.dataset.countdownInterval = String(interval);
    } else {
        value.textContent = "—";
    }
    
    // Clear any existing interval from previous pill
    const oldPill = dom.overviewEl.querySelector(".next-poll-pill");
    if (oldPill?.dataset.countdownInterval) {
        clearInterval(parseInt(oldPill.dataset.countdownInterval, 10));
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
        // If something throws deep inside rendering (e.g. Chart.js internal recursion),
        // capture a readable stack for the status bar and console.
        try {
            render(payload);
        } catch (e) {
            const err = e instanceof Error ? e : new Error(String(e));
            globalThis.__pmfLastRenderError = { message: err.message, stack: err.stack || "" };
            // eslint-disable-next-line no-console
            console.error("[render] failed", err);
            throw err;
        }

        // Find latest timestamp from all points for status display.
        // Avoid `flatMap`/`map` chains: some environments/polyfills can be surprisingly costly,
        // and large data sets can trigger stack overflows in non-native implementations.
        let latestTs = 0;
        const items = payload?.items;
        if (Array.isArray(items)) {
            for (let i = 0; i < items.length; i += 1) {
                const pts = items[i]?.points;
                if (!Array.isArray(pts)) continue;
                for (let j = 0; j < pts.length; j += 1) {
                    const t = pts[j]?.time;
                    if (typeof t === "number" && Number.isFinite(t) && t > latestTs) {
                        latestTs = t;
                    }
                }
            }
        }

        const timeLabel = latestTs ? formatTime(latestTs, true) : formatTime(Date.now(), true);
        const ageMs = latestTs ? Date.now() - latestTs : 0;
        const stale = latestTs && ageMs > 60 * 30 * 1000;
        setStatus(stale ? "warn" : "ok", stale ? `Stale ${timeLabel}` : `Live ${timeLabel}`);
    } catch (error) {
        const err = error instanceof Error ? error : new Error(String(error));
        globalThis.__pmfLastRefreshError = { message: err.message, stack: err.stack || "" };
        // eslint-disable-next-line no-console
        console.error("[refresh] failed", err);
        const stackLine = String(err.stack || "").split("\n").slice(0, 2).join(" · ");
        const msg = err.message || "Unknown error";
        setStatus("error", stackLine ? `Error: ${msg} (${stackLine})` : `Error: ${msg}`);
    }
}
