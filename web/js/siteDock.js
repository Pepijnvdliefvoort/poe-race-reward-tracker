/**
 * Site taskbar for the public pages (dashboard, compare, AA ladder, alt arts): the site's
 * navigation, shown to everyone, with the current page marked.
 *
 * For an authenticated admin (checked via /api/companion/auth) a separate, tinted section
 * with the admin apps follows a divider; visitors never see it. Clicking an admin app opens
 * it as a floating window over the current page (the admin page in an iframe, `?embedded=1`);
 * clicking again minimizes it. The window's own traffic lights talk to this script through
 * postMessage (see web/js/core/macWindow.js). Open windows survive moving between public
 * pages: they are restored minimized on the next page. On phones the dock is a bottom tab
 * bar and admin apps open as normal pages.
 */

const STATE_KEY = "admin.dock.windows.v1"; // sessionStorage: [{ app, hash }]
const MSG_FROM_APP = "mac-window";
const MSG_TO_APP = "mac-window-state";

const svg = (paths) => `<svg viewBox="0 0 20 20" aria-hidden="true">${paths}</svg>`;

// Public pages (links); icons drawn white on a coloured tile.
const PAGES = [
  {
    id: "dashboard",
    name: "Dashboard",
    href: "/",
    svg: svg('<rect x="3" y="3" width="6" height="6" rx="1.3" fill="#fff"/><rect x="11" y="3" width="6" height="6" rx="1.3" fill="#fff"/><rect x="3" y="11" width="6" height="6" rx="1.3" fill="#fff"/><rect x="11" y="11" width="6" height="6" rx="1.3" fill="#fff"/>'),
  },
  {
    id: "compare",
    name: "Compare",
    href: "/compare",
    svg: svg('<path d="M4 16V9M8 16V4M12 16v-6M16 16V7" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>'),
  },
  {
    id: "aa-ladder",
    name: "AA ladder",
    href: "/aa-ladder",
    svg: svg('<path d="M3 17h4v-4h4V9h4V5h2" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'),
  },
  {
    id: "alt-arts",
    name: "Alt arts",
    href: "/alt-arts",
    svg: svg('<rect x="3" y="4" width="14" height="12" rx="2" fill="none" stroke="#fff" stroke-width="1.8"/><circle cx="8" cy="8.5" r="1.6" fill="#fff"/><path d="M4 15l4-4 3 3 2-2 3 3" fill="none" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/>'),
  },
];

function currentPageId() {
  const path = window.location.pathname.replace(/\/+$/, "").replace(/\.html$/, "") || "/";
  if (path === "/" || path === "/index") return "dashboard";
  return PAGES.find((p) => p.href === path)?.id || "";
}

function isPhone() {
  return !!window.matchMedia?.("(max-width: 760px)")?.matches;
}

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
    if (!btn) continue;
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
  if (isPhone()) {
    // No floating windows on phones: open the admin app as a normal page.
    window.location.assign(APPS.find((a) => a.id === appId).path);
    return;
  }
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

function dockItem({ tag, className, label, iconClass, img, svgMarkup }) {
  const item = document.createElement(tag);
  item.className = className;
  const icon = document.createElement("span");
  icon.className = `adock-icon ${iconClass}`;
  if (img) {
    const image = document.createElement("img");
    image.src = img;
    image.alt = "";
    icon.appendChild(image);
  } else {
    icon.innerHTML = svgMarkup;
  }
  const text = document.createElement("span");
  text.className = "adock-label";
  text.textContent = label;
  const dot = document.createElement("span");
  dot.className = "adock-dot";
  item.append(icon, text, dot);
  return item;
}

function renderDock() {
  dockEl = document.createElement("nav");
  dockEl.className = "adock";
  dockEl.setAttribute("aria-label", "Site");
  const current = currentPageId();
  for (const page of PAGES) {
    const link = dockItem({
      tag: "a",
      className: "adock-item adock-item--page",
      label: page.name,
      iconClass: `adock-icon--${page.id}`,
      svgMarkup: page.svg,
    });
    link.href = page.href;
    if (page.id === current) link.setAttribute("aria-current", "page");
    dockEl.appendChild(link);
  }
  adoptThemeToggle();
  document.body.appendChild(dockEl);
  document.body.classList.add("has-site-dock");
  requestAnimationFrame(() => dockEl.classList.add("adock--shown"));
}

// The page's own theme button (#themeToggle, wired up by the page script) joins the dock as a
// utility icon at the far end (like macOS utilities), so the header only needs the title and
// the status pill.
let utilityStart = null; // first node of the utility area; the admin section goes before it

function adoptThemeToggle() {
  const toggle = document.getElementById("themeToggle");
  if (!toggle) return;
  const slot = toggle.parentElement;
  const divider = document.createElement("span");
  divider.className = "adock-divider";
  divider.setAttribute("aria-hidden", "true");
  toggle.classList.add("adock-item", "adock-item--util");
  const icon = toggle.querySelector(".theme-icon");
  icon?.classList.add("adock-icon", "adock-icon--theme");
  const label = document.createElement("span");
  label.className = "adock-label";
  label.textContent = "Appearance";
  const dot = document.createElement("span");
  dot.className = "adock-dot";
  toggle.append(label, dot);
  dockEl.append(divider, toggle);
  utilityStart = divider;
  if (slot && slot.classList.contains("topbar-actions") && !slot.children.length) slot.remove();
}

function renderAdminSection() {
  const divider = document.createElement("span");
  divider.className = "adock-divider";
  divider.setAttribute("aria-hidden", "true");
  const group = document.createElement("div");
  group.className = "adock-admin";
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Admin apps (token required)");
  const badge = document.createElement("span");
  badge.className = "adock-admin-badge";
  badge.title = "Admin only";
  badge.innerHTML = svg('<rect x="5" y="9" width="10" height="8" rx="1.5" fill="currentColor"/><path d="M7 9V7a3 3 0 016 0v2" fill="none" stroke="currentColor" stroke-width="1.8"/>');
  group.appendChild(badge);
  for (const app of APPS) {
    const btn = dockItem({
      tag: "button",
      className: "adock-item",
      label: app.name,
      iconClass: `adock-icon--${app.id}`,
      img: app.img,
      svgMarkup: app.svg,
    });
    btn.type = "button";
    btn.dataset.app = app.id;
    btn.addEventListener("click", () => onDockClick(app.id));
    group.appendChild(btn);
  }
  dockEl.insertBefore(divider, utilityStart);
  dockEl.insertBefore(group, utilityStart);
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
  // Never nest the dock inside an admin window.
  if (window.parent !== window) return;
  renderDock();
  // The admin section only exists for an authenticated admin.
  if (!(await isAdmin())) return;
  renderAdminSection();
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
