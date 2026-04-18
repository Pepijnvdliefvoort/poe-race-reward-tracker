import { REFRESH_MS } from "./js/state.js";
import { initTheme, toggleTheme } from "./js/theme.js";
import { refresh } from "./js/renderer.js";
import { registerFilterEventListeners, registerKeyboardShortcuts, syncFilterControlsFromState } from "./js/filterUI.js";

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

initFiltersDrawer();

// Start data refresh cycle
refresh();
setInterval(refresh, REFRESH_MS);

function initFiltersDrawer() {
  const menuBtn = document.getElementById("filtersMenuBtn");
  const closeBtn = document.getElementById("filtersCloseBtn");
  const overlay = document.getElementById("filtersOverlay");
  const drawer = overlay?.querySelector(".filters-drawer");

  if (!menuBtn || !closeBtn || !overlay || !drawer) {
    return;
  }

  const openDrawer = () => {
    overlay.classList.add("open");
    overlay.setAttribute("aria-hidden", "false");
    menuBtn.setAttribute("aria-expanded", "true");
    window.requestAnimationFrame(() => closeBtn.focus());
  };

  const closeDrawer = () => {
    overlay.classList.remove("open");
    overlay.setAttribute("aria-hidden", "true");
    menuBtn.setAttribute("aria-expanded", "false");
    menuBtn.focus();
  };

  menuBtn.addEventListener("click", openDrawer);
  closeBtn.addEventListener("click", closeDrawer);

  overlay.addEventListener("click", (event) => {
    if (!drawer.contains(event.target)) {
      closeDrawer();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && overlay.classList.contains("open")) {
      closeDrawer();
    }
  });
}
