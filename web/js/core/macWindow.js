/**
 * macOS-style window chrome shared by the admin pages (admin.html, db.html):
 * - red: close the window (animates out, then opens the dashboard)
 * - yellow: minimize to a dock at the bottom; click the dock icon to restore
 * - green: zoom between a floating window and the full browser window (remembered);
 *   double-clicking the sidebar header does the same, like a macOS title bar
 * - the Appearance button toggles light/dark, shared with the dashboard's setting
 */

const ZOOM_KEY = "admin.window.zoomed.v1";
const THEME_KEY = "poe-market-theme";
const CLOSE_TO = "/";

function reducedMotion() {
  return !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
}

function store(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // storage unavailable (private mode / blocked)
  }
}

export function setupMacWindow({ appName = "Admin", iconSrc = "/assets/icons/FairgravesTricorneAlt.png", onMinimizedChange } = {}) {
  const app = document.querySelector(".mac-app");
  const root = document.documentElement;
  let minimized = false;
  if (!app) return { isMinimized: () => false };

  const buttons = {
    close: app.querySelector('[data-window="close"]'),
    minimize: app.querySelector('[data-window="minimize"]'),
    zoom: app.querySelector('[data-window="zoom"]'),
  };

  // ---- zoom ----
  const syncZoomLabel = () => {
    const zoomed = root.classList.contains("mac-zoomed");
    buttons.zoom?.setAttribute("aria-pressed", zoomed ? "true" : "false");
    if (buttons.zoom) buttons.zoom.title = zoomed ? "Exit full window" : "Fill the browser window";
  };
  const toggleZoom = () => {
    const zoomed = root.classList.toggle("mac-zoomed");
    store(ZOOM_KEY, zoomed ? "1" : "0");
    syncZoomLabel();
    // Content that measures itself (maps, editors) needs a resize after the size change.
    window.setTimeout(() => window.dispatchEvent(new Event("resize")), 320);
  };
  syncZoomLabel();
  buttons.zoom?.addEventListener("click", toggleZoom);
  app.querySelector(".mac-sidebar-head")?.addEventListener("dblclick", toggleZoom);

  // ---- close ----
  buttons.close?.addEventListener("click", () => {
    const go = () => window.location.assign(CLOSE_TO);
    if (reducedMotion()) {
      go();
      return;
    }
    app.classList.add("mac-app--closing");
    window.setTimeout(go, 200);
  });

  // ---- minimize to dock ----
  const dock = document.createElement("div");
  dock.className = "mac-dock";
  dock.hidden = true;
  const dockBtn = document.createElement("button");
  dockBtn.type = "button";
  dockBtn.className = "mac-dock-item";
  dockBtn.setAttribute("aria-label", `Restore the ${appName} window`);
  dockBtn.title = `Restore ${appName}`;
  const img = document.createElement("img");
  img.src = iconSrc;
  img.alt = "";
  const label = document.createElement("span");
  label.className = "mac-dock-label";
  label.textContent = appName;
  const dot = document.createElement("span");
  dot.className = "mac-dock-dot";
  dockBtn.append(img, label, dot);
  dock.appendChild(dockBtn);
  document.body.appendChild(dock);

  const setMinimized = (on) => {
    if (on === minimized) return;
    minimized = on;
    if (on) {
      app.classList.add("mac-app--minimized");
      app.setAttribute("aria-hidden", "true");
      app.inert = true;
      dock.hidden = false;
      requestAnimationFrame(() => dock.classList.add("mac-dock--shown"));
      dockBtn.focus({ preventScroll: true });
    } else {
      app.classList.remove("mac-app--minimized");
      app.removeAttribute("aria-hidden");
      app.inert = false;
      dock.classList.remove("mac-dock--shown");
      window.setTimeout(() => {
        if (!minimized) dock.hidden = true;
      }, 250);
      buttons.minimize?.focus({ preventScroll: true });
    }
    onMinimizedChange?.(on);
  };
  buttons.minimize?.addEventListener("click", () => setMinimized(true));
  dockBtn.addEventListener("click", () => setMinimized(false));

  // ---- appearance ----
  document.getElementById("adminThemeToggle")?.addEventListener("click", () => {
    const light = root.classList.toggle("light-theme");
    store(THEME_KEY, light ? "light" : "dark");
  });

  return { isMinimized: () => minimized };
}
