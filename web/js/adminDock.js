/**
 * Admin taskbar for the public pages (dashboard, compare, AA ladder, alt arts).
 *
 * Only rendered for an authenticated admin (checked via /api/companion/auth); visitors get
 * nothing. The dock holds the admin apps. Clicking one opens it as a floating window over the
 * current page (the admin page in an iframe, `?embedded=1`); clicking again minimizes it.
 * The window's own traffic lights talk to this script through postMessage (see
 * web/js/core/macWindow.js). Open windows survive moving between public pages: they are
 * restored minimized on the next page.
 */

const STATE_KEY = "admin.dock.windows.v1"; // sessionStorage: [{ app, hash }]
const MSG_FROM_APP = "mac-window";
const MSG_TO_APP = "mac-window-state";

const APPS = [
  { id: "admin", name: "Admin", path: "/admin", img: "/assets/icons/FairgravesTricorneAlt.png" },
  {
    id: "db",
    name: "DB explorer",
    path: "/admin/db",
    svg: '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="4" width="14" height="12" rx="2" fill="none" stroke="#fff" stroke-width="1.8"/><path d="M3 8h14M8 8v8" fill="none" stroke="#fff" stroke-width="1.6"/></svg>',
  },
];

const windows = new Map(); // app id -> { el, frame, state: "open" | "minimized", zoomed, hash }
let dockEl = null;
let topZ = 2400;

function reducedMotion() {
  return !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
}

function readSaved() {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(STATE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((w) => APPS.some((a) => a.id === w?.app)) : [];
  } catch {
    return [];
  }
}

function saveState() {
  const list = [];
  for (const [app, w] of windows) {
    let hash = w.hash || "";
    try {
      hash = w.frame?.contentWindow?.location.hash || hash;
    } catch {
      // cross-document access failed; keep the last known hash
    }
    list.push({ app, hash });
  }
  try {
    window.sessionStorage.setItem(STATE_KEY, JSON.stringify(list));
  } catch {
    // storage unavailable
  }
}

function iconFor(app) {
  const btn = dockEl?.querySelector(`[data-app="${app}"]`);
  return btn?.querySelector(".adock-icon") || btn;
}

function setOrigin(w, app) {
  // Open/minimize animate from/to the app's dock icon.
  const icon = iconFor(app)?.getBoundingClientRect();
  const box = w.el.getBoundingClientRect();
  if (!icon || !box.width) return;
  const x = icon.left + icon.width / 2 - box.left;
  const y = icon.top + icon.height / 2 - box.top;
  w.el.style.transformOrigin = `${Math.round(x)}px ${Math.round(y)}px`;
}

function syncDock() {
  if (!dockEl) return;
  for (const app of APPS) {
    const btn = dockEl.querySelector(`[data-app="${app.id}"]`);
    const w = windows.get(app.id);
    btn.classList.toggle("adock-item--running", !!w);
    btn.setAttribute(
      "aria-label",
      !w ? `Open ${app.name}` : w.state === "open" ? `Minimize ${app.name}` : `Restore ${app.name}`,
    );
  }
}

function postState(w) {
  try {
    w.frame?.contentWindow?.postMessage({ type: MSG_TO_APP, minimized: w.state !== "open" }, window.location.origin);
  } catch {
    // frame not ready yet; it asks for nothing until it loads
  }
}

function focusWindow(w) {
  topZ += 1;
  w.el.style.zIndex = String(topZ);
}

function ensureFrame(appId, w) {
  if (w.frame) return;
  const app = APPS.find((a) => a.id === appId);
  const frame = document.createElement("iframe");
  frame.className = "adock-frame";
  frame.title = app.name;
  frame.src = `${app.path}?embedded=1${w.hash || ""}`;
  frame.addEventListener("load", () => postState(w));
  w.el.appendChild(frame);
  w.frame = frame;
}

function showWindow(appId, { animate = true } = {}) {
  const w = windows.get(appId);
  if (!w) return;
  ensureFrame(appId, w);
  w.el.hidden = false;
  focusWindow(w);
  setOrigin(w, appId);
  const reveal = () => {
    w.el.classList.remove("adock-window--minimized");
    w.state = "open";
    postState(w);
    syncDock();
    saveState();
  };
  if (animate && !reducedMotion()) {
    w.el.classList.add("adock-window--minimized");
    void w.el.offsetWidth; // commit the start state so the transition runs
    reveal();
  } else {
    reveal();
  }
}

function minimizeWindow(appId) {
  const w = windows.get(appId);
  if (!w || w.state !== "open") return;
  setOrigin(w, appId);
  w.el.classList.add("adock-window--minimized");
  w.state = "minimized";
  postState(w); // lets the app pause its polling
  window.setTimeout(() => {
    if (w.state === "minimized") w.el.hidden = true;
  }, reducedMotion() ? 0 : 380);
  syncDock();
  saveState();
}

function closeWindow(appId) {
  const w = windows.get(appId);
  if (!w) return;
  w.el.classList.add("adock-window--closing");
  windows.delete(appId);
  window.setTimeout(() => w.el.remove(), reducedMotion() ? 0 : 200);
  syncDock();
  saveState();
}

function toggleZoom(appId) {
  const w = windows.get(appId);
  if (!w) return;
  w.zoomed = !w.zoomed;
  w.el.classList.toggle("adock-window--zoomed", w.zoomed);
}

function createWindow(appId, { hash = "", minimized = false } = {}) {
  const app = APPS.find((a) => a.id === appId);
  const el = document.createElement("div");
  el.className = "adock-window adock-window--minimized";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", app.name);
  el.hidden = true;
  // Cascade like macOS: each extra window opens a little down and to the right.
  el.style.setProperty("--cascade", `${windows.size * 28}px`);
  el.addEventListener("pointerdown", () => focusWindow(w), true);
  document.body.appendChild(el);
  const w = { el, frame: null, state: "minimized", zoomed: false, hash };
  windows.set(appId, w);
  if (minimized) {
    syncDock(); // restored lazily: the iframe loads when the window is first opened
    return w;
  }
  showWindow(appId);
  return w;
}

function onDockClick(appId) {
  const w = windows.get(appId);
  if (!w) createWindow(appId);
  else if (w.state === "open") minimizeWindow(appId);
  else showWindow(appId);
}

function onAppMessage(ev) {
  if (ev.origin !== window.location.origin || ev.data?.type !== MSG_FROM_APP) return;
  const entry = [...windows.entries()].find(([, w]) => w.frame?.contentWindow === ev.source);
  if (!entry) return;
  const [appId] = entry;
  const { action } = ev.data;
  if (action === "close") closeWindow(appId);
  else if (action === "minimize") minimizeWindow(appId);
  else if (action === "zoom") toggleZoom(appId);
  else if (action === "open" && APPS.some((a) => a.id === ev.data.app)) {
    // e.g. the DB explorer link inside the admin window opens (or focuses) the DB window
    const target = windows.get(ev.data.app);
    if (!target) createWindow(ev.data.app, { hash: ev.data.hash || "" });
    else {
      if (ev.data.hash && target.frame) target.frame.contentWindow.location.hash = ev.data.hash;
      showWindow(ev.data.app, { animate: target.state !== "open" });
    }
  } else if (action === "navigate" && typeof ev.data.href === "string") {
    const url = new URL(ev.data.href, window.location.origin);
    if (url.origin === window.location.origin) window.location.assign(url.href);
  }
}

function renderDock() {
  dockEl = document.createElement("nav");
  dockEl.className = "adock";
  dockEl.setAttribute("aria-label", "Admin apps");
  for (const app of APPS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "adock-item";
    btn.dataset.app = app.id;
    const icon = document.createElement("span");
    icon.className = `adock-icon adock-icon--${app.id}`;
    if (app.img) {
      const img = document.createElement("img");
      img.src = app.img;
      img.alt = "";
      icon.appendChild(img);
    } else {
      icon.innerHTML = app.svg;
    }
    const label = document.createElement("span");
    label.className = "adock-label";
    label.textContent = app.name;
    const dot = document.createElement("span");
    dot.className = "adock-dot";
    btn.append(icon, label, dot);
    btn.addEventListener("click", () => onDockClick(app.id));
    dockEl.appendChild(btn);
  }
  document.body.appendChild(dockEl);
  requestAnimationFrame(() => dockEl.classList.add("adock--shown"));
}

async function isAdmin() {
  try {
    const res = await fetch("/api/companion/auth", { credentials: "same-origin" });
    if (!res.ok) return false;
    return !!(await res.json())?.authenticated;
  } catch {
    return false;
  }
}

async function init() {
  // Never nest the dock inside an admin window, and never show it to visitors.
  if (window.parent !== window) return;
  if (!(await isAdmin())) return;

  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/css/admin-dock.css?v=20260930-2";
  document.head.appendChild(css);

  renderDock();
  window.addEventListener("message", onAppMessage);
  window.addEventListener("pagehide", saveState);
  document.addEventListener("keydown", (ev) => {
    // Escape minimizes the front window, like hiding a panel.
    if (ev.key !== "Escape") return;
    const front = [...windows.entries()]
      .filter(([, w]) => w.state === "open")
      .sort((a, b) => Number(b[1].el.style.zIndex) - Number(a[1].el.style.zIndex))[0];
    if (front) minimizeWindow(front[0]);
  });

  // Windows that were open on the previous page (or minimized from the standalone admin page).
  for (const saved of readSaved()) createWindow(saved.app, { hash: saved.hash || "", minimized: true });
}

void init();
