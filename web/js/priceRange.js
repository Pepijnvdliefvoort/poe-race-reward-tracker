import { dom, saveFilters, state } from "./state.js";

/**
 * Manages price range slider state, UI synchronization, and updates.
 */

export function setMinLabel(value) {
  dom.priceRangeMinLabel.value = Math.round(value);
}

export function setMaxLabel(value) {
  dom.priceRangeMaxLabel.value = Math.round(value);
  dom.rangeAtCapEl.classList.toggle("visible", value >= state.globalPriceRange.max);
}

export function updateRangeFill() {
  const { min, max } = state.globalPriceRange;
  const range = max - min || 1;
  const leftPct = ((state.filters.priceMin - min) / range) * 100;
  const rightPct = ((state.filters.priceMax - min) / range) * 100;
  dom.rangeFillEl.style.left = `${leftPct}%`;
  dom.rangeFillEl.style.width = `${rightPct - leftPct}%`;
}

/**
 * Initialize or update the price range slider with new min/max bounds.
 */
export function initPriceRangeSlider(newMin = 0, newMax = 100) {
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

/**
 * Sync the price range input elements from current state.
 */
export function syncPriceRangeFromState() {
  dom.priceRangeMinInput.value = state.filters.priceMin;
  dom.priceRangeMaxInput.value = state.filters.priceMax;
  setMinLabel(state.filters.priceMin);
  setMaxLabel(state.filters.priceMax);
  updateRangeFill();
}

/**
 * Handle min slider input change.
 */
export function handlePriceRangeMinChange() {
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
}

/**
 * Handle max slider input change.
 */
export function handlePriceRangeMaxChange() {
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
}

/**
 * Handle manual min label input change.
 */
export function handlePriceRangeMinLabelChange() {
  let val = parseInt(dom.priceRangeMinLabel.value, 10);
  if (Number.isNaN(val)) val = state.globalPriceRange.min;
  val = Math.max(state.globalPriceRange.min, Math.min(val, state.filters.priceMax));
  state.filters.priceMin = val;
  dom.priceRangeMinInput.value = val;
  setMinLabel(val);
  updateRangeFill();
  saveFilters();
}

/**
 * Handle manual max label input change.
 */
export function handlePriceRangeMaxLabelChange() {
  let val = parseInt(dom.priceRangeMaxLabel.value, 10);
  if (Number.isNaN(val)) val = state.globalPriceRange.max;
  val = Math.max(state.filters.priceMin, Math.min(val, state.globalPriceRange.max));
  state.filters.priceMax = val;
  dom.priceRangeMaxInput.value = val;
  setMaxLabel(val);
  updateRangeFill();
  saveFilters();
}
