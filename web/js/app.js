import { REFRESH_MS } from "./core/state.js";
import { initTheme, toggleTheme } from "./ui/theme.js";
import { refresh } from "./ui/renderer.js";
import { registerFilterEventListeners, registerKeyboardShortcuts, syncFilterControlsFromState } from "./ui/filterUI.js";
import { initCompanion } from "./companion/companion.js";

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

initBackToTopButton();
initCompanion();
initStatusJumpToNextInLine();

// Start data refresh cycle
refresh();
setInterval(refresh, REFRESH_MS);


function initBackToTopButton() {
  const btn = document.getElementById("backToTopBtn");
  if (!btn) return;

  const showAfterPx = 520;

  const sync = () => {
    const shouldShow = (window.scrollY || 0) > showAfterPx;
    btn.hidden = !shouldShow;
  };

  btn.addEventListener("click", () => {
    try {
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch {
      window.scrollTo(0, 0);
    }
  });

  window.addEventListener("scroll", sync, { passive: true });
  window.addEventListener("resize", sync, { passive: true });
  sync();
}

function initStatusJumpToNextInLine() {
  const meta = document.getElementById("meta");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  if (!meta || !statusDot || !statusText) return;

  const canJump = () => {
    const text = String(statusText.textContent || "").trim();
    return statusDot.classList.contains("ok") && /^Live\b/i.test(text);
  };

  const updateJumpUi = () => {
    const enabled = canJump();
    meta.classList.toggle("status-jump-enabled", enabled);
    meta.setAttribute("role", "button");
    meta.tabIndex = 0;
    meta.setAttribute("aria-disabled", enabled ? "false" : "true");
    meta.title = enabled
      ? "Jump to current next-in-line item"
      : "Jump is available while status is Live";
  };

  const jumpToNextInLineCard = () => {
    if (!canJump()) return;
    const card = document.querySelector(".card.next-in-line");
    if (!(card instanceof HTMLElement)) return;

    try {
      card.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
    } catch {
      card.scrollIntoView();
    }

    card.classList.remove("card-jump-highlight");
    void card.getBoundingClientRect();
    card.classList.add("card-jump-highlight");
    window.setTimeout(() => {
      card.classList.remove("card-jump-highlight");
    }, 950);
  };

  meta.addEventListener("click", jumpToNextInLineCard);
  meta.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    jumpToNextInLineCard();
  });

  const observer = new MutationObserver(() => updateJumpUi());
  observer.observe(statusDot, { attributes: true, attributeFilter: ["class"] });
  observer.observe(statusText, { childList: true, characterData: true, subtree: true });

  updateJumpUi();
}
