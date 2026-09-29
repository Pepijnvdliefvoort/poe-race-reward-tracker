/**
 * macOS-style window chrome shared by the admin pages (admin.html, db.html).
 *
 * Standalone (opened directly at /admin or /admin/db):
 * - red: close (animates out, then opens the dashboard)
 * - yellow: minimize to the dashboard's dock (web/js/siteDock.js restores it there)
 * - green: zoom between a floating window and the full browser window (remembered);
 *   double-clicking the title area does the same, like a macOS title bar
 *
 * Embedded (inside a dock window on a public page, `?embedded=1`): the buttons ask the
 * dock to close / minimize / zoom this window via postMessage, links to the other admin
 * app open that app's window, public links navigate the whole page, and the dock tells
 * the page when it is minimized so polling can pause.
 *
 * The Appearance button toggles light/dark, shared with the dashboard's setting.
 */

const ZOOM_KEY = "admin.window.zoomed.v1";
const THEME_KEY = "poe-market-theme";
const DOCK_STATE_KEY = "admin.dock.windows.v1"; // read by siteDock.js
const MSG_TO_DOCK = "mac-window";
const MSG_FROM_DOCK = "mac-window-state";
const DASHBOARD = "/";

function reducedMotion() {
  return !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
}

function store(storage, key, value) {
  try {
    storage.setItem(key, value);
  } catch {
    // storage unavailable (private mode / blocked)
  }
}

// Double-clicking the title area (sidebar header, or the title bar of a sidebar-less app)
// zooms, like a macOS title bar; double-clicks on its buttons are ignored.
function onTitleDoubleClick(app, handler) {
  app.querySelectorAll(".mac-sidebar-head, .mac-titlebar").forEach((el) =>
    el.addEventListener("dblclick", (ev) => {
      if (!ev.target?.closest?.("button, a, input")) handler();
    }),
  );
}

function appIdForPath(pathname) {
  return pathname.replace(/\/+$/, "") === "/admin/db" ? "db" : "admin";
}

export function setupMacWindow({ onMinimizedChange } = {}) {
  const app = document.querySelector(".mac-app");
  const root = document.documentElement;
  if (!app) return;

  const embedded = window.parent !== window && new URLSearchParams(window.location.search).has("embedded");
  const thisApp = appIdForPath(window.location.pathname);
  const buttons = {
    close: app.querySelector('[data-window="close"]'),
    minimize: app.querySelector('[data-window="minimize"]'),
    zoom: app.querySelector('[data-window="zoom"]'),
  };
  const toDock = (msg) => window.parent.postMessage({ type: MSG_TO_DOCK, ...msg }, window.location.origin);

  // ---- appearance ----
  document.getElementById("adminThemeToggle")?.addEventListener("click", () => {
    const light = root.classList.toggle("light-theme");
    store(window.localStorage, THEME_KEY, light ? "light" : "dark");
  });

  // Escape closes the window, like the red button. Keys pressed inside an embedded window never
  // reach the dock's own listener, so each app handles it. A focused field gets Escape first
  // (clears/blurs it); the next press closes.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape" || ev.defaultPrevented || ev.repeat || ev.isComposing) return;
    const target = ev.target;
    if (target?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) {
      target.blur?.();
      return;
    }
    if (document.querySelector("dialog[open]")) return;
    buttons.close?.click();
  });

  if (embedded) {
    root.classList.add("mac-embedded");
    root.classList.remove("mac-zoomed"); // the dock window owns its size
    buttons.close?.addEventListener("click", () => toDock({ action: "close" }));
    buttons.minimize?.addEventListener("click", () => toDock({ action: "minimize" }));
    buttons.zoom?.addEventListener("click", () => toDock({ action: "zoom" }));
    onTitleDoubleClick(app, () => toDock({ action: "zoom" }));
    if (buttons.close) buttons.close.title = "Close window";
    if (buttons.minimize) buttons.minimize.title = "Minimize to the dock";
    if (buttons.zoom) buttons.zoom.title = "Zoom";

    // Links: stay inside this window for this app, hand everything else to the dock.
    app.addEventListener("click", (ev) => {
      const a = ev.target?.closest?.("a[href]");
      if (!a || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
      const url = new URL(a.getAttribute("href"), window.location.href);
      if (url.origin !== window.location.origin) return;
      ev.preventDefault();
      if (url.pathname.startsWith("/admin")) {
        const target = appIdForPath(url.pathname);
        if (target === thisApp) window.location.hash = url.hash;
        else toDock({ action: "open", app: target, hash: url.hash });
      } else {
        toDock({ action: "navigate", href: url.href });
      }
    });

    window.addEventListener("message", (ev) => {
      if (ev.origin !== window.location.origin || ev.source !== window.parent) return;
      if (ev.data?.type === MSG_FROM_DOCK) onMinimizedChange?.(!!ev.data.minimized);
    });
    return;
  }

  // ---- standalone: zoom ----
  const syncZoomLabel = () => {
    const zoomed = root.classList.contains("mac-zoomed");
    buttons.zoom?.setAttribute("aria-pressed", zoomed ? "true" : "false");
    if (buttons.zoom) buttons.zoom.title = zoomed ? "Exit full window" : "Fill the browser window";
  };
  const toggleZoom = () => {
    const zoomed = root.classList.toggle("mac-zoomed");
    store(window.localStorage, ZOOM_KEY, zoomed ? "1" : "0");
    syncZoomLabel();
    // Content that measures itself (maps, editors) needs a resize after the size change.
    window.setTimeout(() => window.dispatchEvent(new Event("resize")), 320);
  };
  syncZoomLabel();
  buttons.zoom?.addEventListener("click", toggleZoom);
  onTitleDoubleClick(app, toggleZoom);

  // ---- standalone: close / minimize both lead to the dashboard ----
  const leave = (cls, afterAnimation) => {
    if (reducedMotion()) {
      afterAnimation();
      return;
    }
    app.classList.add(cls);
    window.setTimeout(afterAnimation, cls === "mac-app--minimized" ? 380 : 200);
  };
  buttons.close?.addEventListener("click", () => leave("mac-app--closing", () => window.location.assign(DASHBOARD)));
  buttons.minimize?.addEventListener("click", () =>
    leave("mac-app--minimized", () => {
      // The dashboard's dock picks this up and shows the app minimized, ready to restore.
      let saved = [];
      try {
        saved = JSON.parse(window.sessionStorage.getItem(DOCK_STATE_KEY) || "[]");
      } catch {
        saved = [];
      }
      const others = Array.isArray(saved) ? saved.filter((w) => w?.app !== thisApp) : [];
      store(window.sessionStorage, DOCK_STATE_KEY, JSON.stringify([...others, { app: thisApp, hash: window.location.hash }]));
      window.location.assign(DASHBOARD);
    }),
  );
}
