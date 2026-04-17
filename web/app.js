import { REFRESH_MS } from "./js/state.js";
import { initTheme, toggleTheme } from "./js/theme.js";
import { refresh } from "./js/renderer.js";
import { registerFilterEventListeners, registerKeyboardShortcuts, syncFilterControlsFromState } from "./js/filterUI.js";
import { initSettingsModal } from "./js/settings.js";

/**
 * Main application entry point.
 * Orchestrates initialization of all modules and starts the refresh loop.
 */

// Initialize theme
initTheme();

// Register theme toggle
const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  themeToggle.addEventListener("click", toggleTheme);
}

// Initialize filter UI
syncFilterControlsFromState();
registerFilterEventListeners();
registerKeyboardShortcuts();

// Initialize settings modal
initSettingsModal();

// Start data refresh cycle
refresh();
setInterval(refresh, REFRESH_MS);
