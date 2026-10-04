// Leaflet heatmap rendering can call getImageData frequently; hint to browsers to optimize readbacks.
(() => {
  const orig = HTMLCanvasElement.prototype.getContext;
  if (typeof orig !== "function") return;
  HTMLCanvasElement.prototype.getContext = function getContextPatched(type, options) {
    if (type === "2d") {
      if (!options) {
        return orig.call(this, type, { willReadFrequently: true });
      }
      if (typeof options === "object" && options.willReadFrequently == null) {
        return orig.call(this, type, { ...options, willReadFrequently: true });
      }
    }
    return orig.call(this, type, options);
  };
})();

import { setupMacWindow } from "./core/macWindow.js";
import { confirmSheet, formatWhen, toast } from "./core/macUi.js";

const fetchOpts = { credentials: "same-origin" };

async function fetchJson(path) {
  const res = await fetch(path, fetchOpts);
  if (res.status === 403) {
    const err = new Error("Forbidden");
    err.status = 403;
    throw err;
  }
  if (res.status === 429) {
    const ra = res.headers.get("Retry-After");
    const err = new Error(
      ra
        ? `Too many failed authentication attempts. Retry after ${ra}s.`
        : "Too many failed authentication attempts. Try again later.",
    );
    err.status = 429;
    throw err;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchText(path) {
  const res = await fetch(path, fetchOpts);
  if (res.status === 403) {
    const err = new Error("Forbidden");
    err.status = 403;
    throw err;
  }
  if (res.status === 429) {
    const ra = res.headers.get("Retry-After");
    const err = new Error(
      ra
        ? `Too many failed authentication attempts. Retry after ${ra}s.`
        : "Too many failed authentication attempts. Try again later.",
    );
    err.status = 429;
    throw err;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

async function fetchJsonWithInit(path, init) {
  const res = await fetch(path, { ...fetchOpts, ...(init || {}) });
  if (res.status === 403) {
    const err = new Error("Forbidden");
    err.status = 403;
    throw err;
  }
  if (res.status === 429) {
    const ra = res.headers.get("Retry-After");
    const err = new Error(
      ra
        ? `Too many failed authentication attempts. Retry after ${ra}s.`
        : "Too many failed authentication attempts. Try again later.",
    );
    err.status = 429;
    throw err;
  }
  // Admin endpoints often return a JSON `{ ok: false, error: "..." }` payload even on non-2xx.
  // Prefer surfacing that payload to the UI instead of throwing a generic `HTTP NNN`.
  let data = null;
  try {
    data = await res.json();
  } catch (_e) {
    data = null;
  }
  if (!res.ok) {
    if (data && typeof data === "object") return data;
    throw new Error(`HTTP ${res.status}`);
  }
  return data;
}

/** Human-readable hint for admin API failures (403 / 429). */
function adminEndpointErrorMessage(err, label) {
  if (err.status === 403) {
    return "Unauthorized";
  }
  if (err.status === 429) {
    return err.message || "Too many failed authentication attempts. Try again later.";
  }
  return `${label}: ${err.message || err}`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

let map;
let heatLayer;
let markersLayer;
let visitorMarkersByIp = {};
let lastVisitorMapData = null;
let userFocusedVisitor = false;
const visitorTableSort = { key: "visits", direction: "desc" };
const VISITOR_TABLE_LIMIT = 10;

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

function normalizeVisits(visits, minVisits, maxVisits) {
  // Use log scaling so a single huge IP doesn't flatten the gradient.
  const v = Math.max(0, Number(visits) || 0);
  const minV = Math.max(0, Number(minVisits) || 0);
  const maxV = Math.max(minV, Number(maxVisits) || 0);
  if (maxV <= minV) return 1;

  const ln = (n) => Math.log(1 + Math.max(0, n));
  const t = (ln(v) - ln(minV)) / Math.max(1e-9, ln(maxV) - ln(minV));
  return clamp01(t);
}

function markerStyleForVisits(visits, minVisits, maxVisits) {
  const t = normalizeVisits(visits, minVisits, maxVisits);

  // Color ramp (cool -> hot). Keep size constant; adjust color + opacity only.
  // Prefer a high-contrast "cold" color so low-visit dots remain visible on the (bluish) basemap.
  // hue: 140 (green) -> 18 (orange/red)
  // sat: 70% -> 95%
  // light: 58% -> 45%
  const hue = lerp(140, 18, t);
  const sat = lerp(70, 95, t);
  const light = lerp(58, 45, t);

  // Increase minimum opacity/outline so 1-2 visits are still readable.
  const fillOpacity = lerp(0.55, 0.9, t);
  const strokeOpacity = lerp(0.85, 0.95, t);
  const weight = Math.round(lerp(2, 3, t));

  return {
    radius: 7,
    color: `hsla(${hue} ${Math.round(sat)}% ${Math.round(light - 18)}% / ${strokeOpacity.toFixed(3)})`,
    weight,
    fillColor: `hsl(${hue} ${Math.round(sat)}% ${Math.round(light)}%)`,
    fillOpacity,
  };
}

function formatLocalDateTime(value) {
  if (!value) return "";
  const d = value instanceof Date ? value : new Date(String(value));
  if (Number.isNaN(d.getTime())) return String(value);

  const pad2 = (n) => String(n).padStart(2, "0");
  const dd = pad2(d.getDate());
  const mm = pad2(d.getMonth() + 1);
  const yyyy = String(d.getFullYear());
  const HH = pad2(d.getHours());
  const min = pad2(d.getMinutes());
  return `${dd}/${mm}/${yyyy} ${HH}:${min}`;
}

function ensureMap() {
  const el = document.getElementById("visitorMap");
  if (!el || map) return;
  map = L.map(el, { worldCopyJump: true }).setView([20, 0], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
    maxZoom: 19,
  }).addTo(map);
  markersLayer = L.layerGroup().addTo(map);
}

function renderVisitorMap(data) {
  ensureMap();
  const points = data.points || [];
  lastVisitorMapData = data;
  visitorMarkersByIp = {};
  if (heatLayer) {
    map.removeLayer(heatLayer);
    heatLayer = null;
  }
  markersLayer.clearLayers();

  // Point-only rendering (no heat scaling).

  const visitCounts = points.map((p) => Number(p?.visits) || 0);
  const minVisits = visitCounts.length ? Math.min(...visitCounts) : 0;
  const maxVisits = visitCounts.length ? Math.max(...visitCounts) : 0;

  points.forEach((p) => {
    const m = L.circleMarker([p.lat, p.lng], markerStyleForVisits(p.visits, minVisits, maxVisits));
    const lastSeen = p.lastSeen ? formatLocalDateTime(p.lastSeen) : "";
    m.bindPopup(
      `<strong>${escapeHtml(p.ip)}</strong><br/>Visits: ${p.visits}<br/>${lastSeen ? escapeHtml(lastSeen) : ""}`,
      // Keep the marker centered when focusing an IP. Leaflet popups auto-pan by default,
      // which nudges the map so the popup fits in view (making the marker not centered).
      { autoPan: false },
    );
    markersLayer.addLayer(m);
    const ip = String(p?.ip || "").trim();
    if (ip) visitorMarkersByIp[ip] = m;
  });
}

function renderVisitorTable(data) {
  const body = document.getElementById("visitorTableBody");
  const table = document.getElementById("visitorTable");
  const statsEl = document.getElementById("visitorStats");
  if (!body || !table) return;

  const allPoints = Array.isArray(data?.points) ? data.points.slice() : [];
  if (statsEl) {
    const total = allPoints.length;
    const suffix = total > VISITOR_TABLE_LIMIT ? ` (table shows top ${VISITOR_TABLE_LIMIT})` : "";
    statsEl.textContent = `${total} visitor location${total === 1 ? "" : "s"}${suffix}`;
  }

  const parseDate = (value) => {
    if (!value) return 0;
    const d = new Date(String(value));
    return Number.isNaN(d.getTime()) ? 0 : d.getTime();
  };

  const sortedPoints = allPoints.sort((a, b) => {
    const key = visitorTableSort.key;
    const dir = visitorTableSort.direction === "asc" ? 1 : -1;
    if (key === "ip") {
      return dir * String(a?.ip || "").localeCompare(String(b?.ip || ""));
    }
    if (key === "lastSeen") {
      return dir * (parseDate(a?.lastSeen) - parseDate(b?.lastSeen));
    }
    return dir * ((Number(a?.visits) || 0) - (Number(b?.visits) || 0));
  });

  const points = sortedPoints.slice(0, VISITOR_TABLE_LIMIT);

  body.innerHTML = points
    .map((row) => {
      const ip = String(row?.ip || "");
      const visits = Number(row?.visits) || 0;
      const lastSeen = formatWhen(row?.lastSeen);
      return `
        <tr data-visitor-ip="${escapeHtml(ip)}">
          <td>${escapeHtml(ip)}</td>
          <td>${escapeHtml(visits)}</td>
          <td title="${escapeHtml(lastSeen.title)}">${escapeHtml(lastSeen.text)}</td>
        </tr>`;
    })
    .join("");

  body.querySelectorAll("tr[data-visitor-ip]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const ip = tr.getAttribute("data-visitor-ip") || "";
      const marker = visitorMarkersByIp[ip];
      if (!marker || !map) return;
      userFocusedVisitor = true;
      try {
        map.setView(marker.getLatLng(), Math.max(map.getZoom(), 5), { animate: true });
        marker.openPopup();
      } catch {
        // ignore focus failures
      }
    });
  });

  const headers = Array.from(table.querySelectorAll("thead th"));
  headers.forEach((th) => {
    const btn = th.querySelector("button[data-visitor-sort-key]");
    if (!btn) return;
    const key = btn.getAttribute("data-visitor-sort-key") || "";
    th.setAttribute(
      "aria-sort",
      visitorTableSort.key !== key ? "none" : (visitorTableSort.direction === "asc" ? "ascending" : "descending"),
    );
    if (btn.dataset.boundSortClick === "1") return;
    btn.dataset.boundSortClick = "1";
    btn.addEventListener("click", () => {
      if (visitorTableSort.key === key) {
        visitorTableSort.direction = visitorTableSort.direction === "asc" ? "desc" : "asc";
      } else {
        visitorTableSort.key = key;
        visitorTableSort.direction = key === "ip" ? "asc" : "desc";
      }
      renderVisitorTable(lastVisitorMapData || { points: allPoints });
    });
  });
}

// ---- sections (sidebar navigation) ----------------------------------------------------------

const PANES = ["overview", "logs", "config", "ml", "visitors", "data"];
const PANE_STORAGE_KEY = "admin.pane.v2";
let activePane = null;
let windowMinimized = false;

function paneFromLocation() {
  const fromHash = (window.location.hash || "").replace(/^#/, "");
  if (PANES.includes(fromHash)) return fromHash;
  try {
    const saved = window.localStorage.getItem(PANE_STORAGE_KEY);
    if (PANES.includes(saved)) return saved;
  } catch {
    // storage unavailable
  }
  return "overview";
}

function isPaneLive(name) {
  return activePane === name && !document.hidden && !windowMinimized;
}

function onPaneShown(name) {
  if (name === "logs") {
    requestLogSnapshot();
    [serverLogViewer, pollerLogViewer].forEach((v) => v?.follow && v.scrollToBottom());
  } else if (name === "overview") {
    void refreshStats();
    void refreshPollerStatus();
  } else if (name === "visitors") {
    void refreshVisitors();
    // Leaflet measures its container; it was hidden until now.
    requestAnimationFrame(() => map?.invalidateSize({ animate: false }));
  }
}

function showPane(name, { updateHash = true } = {}) {
  const pane = PANES.includes(name) ? name : "overview";
  activePane = pane;
  document.querySelectorAll(".mac-pane").forEach((el) => {
    el.hidden = el.dataset.pane !== pane;
  });
  document.querySelectorAll(".mac-nav-item[data-pane]").forEach((btn) => {
    if (btn.dataset.pane === pane) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
  try {
    window.localStorage.setItem(PANE_STORAGE_KEY, pane);
  } catch {
    // storage unavailable
  }
  if (updateHash && window.location.hash !== `#${pane}`) {
    window.history.replaceState(null, "", `#${pane}`);
  }
  onPaneShown(pane);
  // Sections that size themselves from their content (hidden until now) re-measure on this.
  document.dispatchEvent(new CustomEvent("admin:pane-shown", { detail: pane }));
}

function setupPanes() {
  document.querySelectorAll(".mac-nav-item[data-pane]").forEach((btn) => {
    btn.addEventListener("click", () => showPane(btn.dataset.pane));
    const head = document.querySelector(`.mac-pane[data-pane="${btn.dataset.pane}"] .mac-pane-head`);
    const icon = btn.querySelector(".mac-nav-icon");
    if (head && icon && !head.querySelector(".mac-pane-icon")) {
      const big = icon.cloneNode(true);
      big.classList.add("mac-pane-icon");
      head.prepend(big);
    }
  });
  window.addEventListener("hashchange", () => showPane(paneFromLocation(), { updateHash: false }));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && activePane) onPaneShown(activePane);
  });
  showPane(paneFromLocation());
}

// ---- logs ------------------------------------------------------------------------------------

// Lines per console snapshot. Each refresh only appends the new lines to the DOM; once a following
// console grows well past this it reloads a fresh snapshot, so a long session stays cheap.
const LOG_MAX_LINES = 3000;
// Scrolling to the top loads older lines (past cycles and sessions) a page at a time, up to a cap.
const LOG_PAGE_LINES = 1500;
const LOG_HISTORY_MAX_LINES = 40000;
const LOG_LOAD_OLDER_PX = 300;
const LOG_POLL_MS = 1500;
const LOG_VIEW_STORAGE_KEY = "admin.logs.view.v2";
const LOG_FOLLOW_STORAGE_KEY = "admin.logs.follow.v2";
const logTimeFormat = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function logLineParts(entry) {
  const raw = String(entry?.ts || "").trim();
  let ts = "";
  if (raw) {
    // Timestamps without a zone are UTC (browsers would read them as local time).
    const d = new Date(/([zZ]|[+-]\d{2}:\d{2})$/.test(raw) ? raw : `${raw}Z`);
    ts = Number.isNaN(d.getTime()) ? raw : logTimeFormat.format(d);
  }
  const lvl = String(entry?.level || "info").toLowerCase();
  const level = lvl === "warning" || lvl === "warn" ? "warn" : lvl === "error" || lvl === "critical" ? "error" : "info";
  const parts = [ts, entry?.msg ? String(entry.msg) : ""];
  if (level === "error" && entry?.exc) parts.push(String(entry.exc));
  return { level, text: parts.filter(Boolean).join(" ") || "(blank)" };
}

class LogViewer {
  constructor({ name, preEl, toolbarEl, paneEl, onFilterChange }) {
    this.name = name;
    this.preEl = preEl;
    this.toolbarEl = toolbarEl;
    this.paneEl = paneEl;
    this.onFilterChange = onFilterChange;
    this.level = "all"; // all | info | warn | error
    this.query = "";
    this.cursor = null; // null = next request is a full snapshot
    this.needsSnapshot = false; // set when (re)opened, so it shows the latest lines, not a catch-up
    this.counts = null;
    this._filterKey = "";
    this.lineCount = 0;
    this.placeholder = false;
    this.follow = true;
    this.before = null; // file offset to page back from (0 = start of the log reached)
    this.detached = 0; // leading warnings/errors from before the snapshot's contiguous tail
    this.loadingOlder = false;
    this.generation = 0; // bumped on each snapshot so stale older pages are dropped
    this.onNeedOlder = null;
    this.pills = {};
    this._setupToolbar();
    this._setupJump();
  }

  _setupToolbar() {
    if (!this.toolbarEl) return;
    this.toolbarEl.textContent = "";
    const label = document.createElement("span");
    label.className = "mac-log-name";
    label.textContent = `${this.name}.log`;
    this.toolbarEl.appendChild(label);

    const levels = [
      ["all", "All", ""],
      ["info", "Info", ""],
      ["warn", "Warning", "admin-pill--warn"],
      ["error", "Error", "admin-pill--error"],
    ];
    for (const [key, text, cls] of levels) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `admin-pill ${cls}`.trim();
      btn.textContent = text;
      btn.addEventListener("click", () => {
        if (this.level === key) return;
        this.level = key;
        this._syncPressed();
        this._filtersChanged();
      });
      this.toolbarEl.appendChild(btn);
      this.pills[key] = btn;
    }

    const input = document.createElement("input");
    input.className = "admin-pill-input";
    input.type = "search";
    input.placeholder = "Filter";
    input.setAttribute("aria-label", `Filter ${this.name}.log`);
    let t = null;
    input.addEventListener("input", () => {
      window.clearTimeout(t);
      t = window.setTimeout(() => {
        this.query = input.value || "";
        this._filtersChanged();
      }, 250);
    });
    this.toolbarEl.appendChild(input);

    const tool = (label, title, onClick) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mac-log-tool";
      b.textContent = label;
      b.title = title;
      b.setAttribute("aria-label", title);
      b.addEventListener("click", onClick);
      this.toolbarEl.appendChild(b);
    };
    tool("⧉", `Copy the visible ${this.name}.log lines`, () => void this.copyVisible());
    tool("↓", `Download the visible ${this.name}.log lines`, () => this.downloadVisible());
    this._syncPressed();
  }

  visibleText() {
    return this.placeholder ? "" : this.preEl?.textContent || "";
  }

  async copyVisible() {
    const text = this.visibleText();
    if (!text) {
      toast("Nothing to copy.", { kind: "info" });
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied ${this.lineCount} ${this.name}.log line${this.lineCount === 1 ? "" : "s"}.`);
    } catch {
      toast("Copy failed: clipboard access was blocked.", { kind: "error" });
    }
  }

  downloadVisible() {
    const text = this.visibleText();
    if (!text) {
      toast("Nothing to download.", { kind: "info" });
      return;
    }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    a.download = `${this.name}-${stamp}.log`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  _setupJump() {
    if (!this.paneEl || !this.preEl) return;
    this.jumpBtn = document.createElement("button");
    this.jumpBtn.type = "button";
    this.jumpBtn.className = "mac-log-jump";
    this.jumpBtn.textContent = "↓ Latest";
    this.jumpBtn.hidden = true;
    this.jumpBtn.addEventListener("click", () => this.scrollToBottom());
    this.paneEl.appendChild(this.jumpBtn);
    this.preEl.addEventListener(
      "scroll",
      () => {
        if (this.isNearBottom()) this.jumpBtn.hidden = true;
        if (this.canLoadOlder() && this.preEl.scrollTop < LOG_LOAD_OLDER_PX) this.onNeedOlder?.(this);
      },
      { passive: true },
    );
    // A short console cannot scroll, so the top marker also loads older lines on click.
    this.preEl.addEventListener("click", (e) => {
      if (e.target.closest?.(".log-top-marker") && this.canLoadOlder()) this.onNeedOlder?.(this);
    });
  }

  _syncPressed() {
    for (const [key, btn] of Object.entries(this.pills)) {
      btn.setAttribute("aria-pressed", key === this.level ? "true" : "false");
    }
  }

  _filtersChanged() {
    this._filterKey = ""; // forces a snapshot request with the new filters
    this.onFilterChange?.();
  }

  get filterKey() {
    return `${this.level}::${this.query}`;
  }

  isNearBottom() {
    const el = this.preEl;
    return el.scrollHeight - (el.scrollTop + el.clientHeight) < 32;
  }

  scrollToBottom() {
    if (!this.preEl) return;
    this.preEl.scrollTop = this.preEl.scrollHeight;
    if (this.jumpBtn) this.jumpBtn.hidden = true;
  }

  reset() {
    this.cursor = null;
    this._filterKey = "";
    this.lineCount = 0;
    this.before = null;
    this.detached = 0;
    this.generation += 1;
    if (this.preEl) this.preEl.textContent = "";
  }

  canLoadOlder() {
    return !this.loadingOlder && this.before > 0 && this.lineCount < LOG_HISTORY_MAX_LINES;
  }

  _mutedLine(text, cls = "") {
    const span = document.createElement("span");
    span.className = `log-line--muted ${cls}`.trim();
    span.textContent = `${text}\n`;
    return span;
  }

  // Marks the top of the console: where older lines load, or the start of the log file.
  _syncTopMarker() {
    if (!this.preEl) return;
    this.preEl.querySelector(":scope > .log-top-marker")?.remove();
    if (this.before == null) return;
    let text = "";
    if (this.before === 0) text = `— start of ${this.name}.log —`;
    else if (this.lineCount >= LOG_HISTORY_MAX_LINES) text = "— history limit reached; use the filter to narrow it down —";
    else if (this.loadingOlder) text = this.query.trim() ? "Searching earlier lines…" : "Loading earlier lines…";
    else text = "↑ Scroll up (or click here) for earlier lines";
    this.preEl.insertBefore(this._mutedLine(text, "log-top-marker"), this.preEl.firstChild);
  }

  _fragment(entries) {
    const frag = document.createDocumentFragment();
    const q = this.query.trim();
    // Highlight what the filter matched (case-insensitive, like the server-side filter).
    const re = q ? new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi") : null;
    for (const entry of entries) {
      const { level, text } = logLineParts(entry);
      const span = document.createElement("span");
      if (level !== "info") span.className = `log-line--${level}`;
      if (re) {
        let last = 0;
        for (const m of text.matchAll(re)) {
          if (m.index > last) span.appendChild(document.createTextNode(text.slice(last, m.index)));
          const mark = document.createElement("mark");
          mark.className = "log-match";
          mark.textContent = m[0];
          span.appendChild(mark);
          last = m.index + m[0].length;
        }
        span.appendChild(document.createTextNode(`${text.slice(last)}\n`));
      } else {
        span.textContent = `${text}\n`;
      }
      frag.appendChild(span);
    }
    return frag;
  }

  setEntries(entries, { before = null, detached = 0 } = {}) {
    if (!this.preEl) return;
    const list = Array.isArray(entries) ? entries.slice(-LOG_MAX_LINES) : [];
    this.generation += 1;
    this.loadingOlder = false;
    this.before = Number.isFinite(before) ? before : null;
    this.detached = Number.isFinite(detached) ? Math.max(0, Math.min(detached, list.length)) : 0;
    this.preEl.textContent = "";
    this.lineCount = list.length;
    this.placeholder = list.length === 0;
    if (this.placeholder) {
      this.preEl.appendChild(this._mutedLine("(no matching lines)"));
    } else {
      this.preEl.appendChild(this._fragment(list));
      if (this.detached) {
        // Warnings/errors kept from earlier in the session; they reload in order with older lines.
        const gap = this._mutedLine("··· earlier lines hidden, scroll up to load them ···", "log-gap");
        this.preEl.insertBefore(gap, this.preEl.children[this.detached] || null);
      }
    }
    this._syncTopMarker();
    if (this.follow) this.scrollToBottom();
  }

  // Inserts an older page above the current lines without moving what is on screen.
  prependEntries(entries, before) {
    if (!this.preEl) return;
    const el = this.preEl;
    const prevHeight = el.scrollHeight;
    const prevTop = el.scrollTop;
    el.querySelector(":scope > .log-top-marker")?.remove();
    if (this.detached) {
      // The older page covers the detached lines too, so drop them (and the gap marker).
      for (let i = 0; i < this.detached && el.firstChild; i += 1) el.removeChild(el.firstChild);
      el.querySelector(":scope > .log-gap")?.remove();
      this.lineCount -= this.detached;
      this.detached = 0;
    }
    if (Array.isArray(entries) && entries.length) {
      if (this.placeholder) {
        el.textContent = "";
        this.lineCount = 0;
        this.placeholder = false;
      }
      el.insertBefore(this._fragment(entries), el.firstChild);
      this.lineCount += entries.length;
    }
    this.before = Number.isFinite(before) ? before : 0;
    this._syncTopMarker();
    el.scrollTop = prevTop + (el.scrollHeight - prevHeight);
  }

  appendEntries(entries) {
    if (!this.preEl || !Array.isArray(entries) || !entries.length) return;
    const stick = this.follow && this.isNearBottom();
    if (this.placeholder) {
      this.preEl.textContent = "";
      this.lineCount = 0;
      this.placeholder = false;
    }
    this.preEl.appendChild(this._fragment(entries));
    this.lineCount += entries.length;
    // Trimming the top would break paging back, so a console following the bottom swaps in a
    // fresh snapshot instead; one scrolled up into history keeps its lines while it is read.
    if ((stick && this.lineCount > LOG_MAX_LINES * 1.5) || this.lineCount > LOG_HISTORY_MAX_LINES + LOG_MAX_LINES) {
      this.needsSnapshot = true;
    }
    if (stick) this.scrollToBottom();
    else if (this.jumpBtn) this.jumpBtn.hidden = false;
  }

  addCounts(delta) {
    if (!this.counts || !delta || typeof delta !== "object") return;
    const next = { ...this.counts };
    for (const key of ["all", "info", "warning", "error", "other"]) {
      next[key] = (Number.isFinite(next[key]) ? next[key] : 0) + (Number.isFinite(delta[key]) ? delta[key] : 0);
    }
    this.setCounts(next);
  }

  setCounts(counts) {
    if (!counts || typeof counts !== "object") return;
    this.counts = counts;
    const n = (v) => (Number.isFinite(v) ? v : 0);
    const labels = {
      all: `All ${n(counts.all)}`,
      info: `Info ${n(counts.info)}`,
      warn: `Warning ${n(counts.warning)}`,
      error: `Error ${n(counts.error)}`,
    };
    for (const [key, text] of Object.entries(labels)) {
      if (this.pills[key]) this.pills[key].textContent = text;
    }
  }
}

function isEditingAdminDatalist() {
  // Re-rendering while a data-tools select is open can dismiss it; pause background refreshes then.
  const el = document.activeElement;
  return typeof el?.closest === "function" && !!el.closest(".admin-data-tools");
}

let serverLogViewer;
let pollerLogViewer;
let logRefreshInFlight = false;
let logsPaused = false;
let logRefreshQueued = false;

function appendLocalConsoleLine(viewer, { msg, level = "info" }) {
  viewer?.appendEntries([{ ts: new Date().toISOString(), level, msg, name: "admin" }]);
}

function visibleLogViewers() {
  const view = document.getElementById("adminLogPanes")?.dataset.view || "split";
  return [serverLogViewer, pollerLogViewer].filter((v) => v && (view === "split" || view === v.name));
}

async function refreshLogStream(viewer) {
  const filterKey = viewer.filterKey;
  const snapshot = viewer.needsSnapshot || viewer.cursor == null || viewer._filterKey !== filterKey;
  if (snapshot) viewer.needsSnapshot = false;
  const params = new URLSearchParams({
    stream: viewer.name,
    format: "json",
    since: "session",
    level: viewer.level,
    q: viewer.query,
    limit: String(LOG_MAX_LINES),
    counts: snapshot ? "1" : "0", // deltas carry deltaCounts, so counts stay live without a recount
  });
  if (!snapshot) params.set("cursor", String(viewer.cursor));
  const payload = await fetchJson(`/api/admin/logs?${params.toString()}`);
  if (payload?.format !== "jsonl" || viewer.filterKey !== filterKey) return; // filters changed meanwhile
  viewer.cursor = Number.isFinite(payload.cursor) ? payload.cursor : viewer.cursor ?? 0;
  viewer._filterKey = filterKey;
  if (!snapshot && payload.delta) {
    viewer.appendEntries(payload.entries);
    viewer.addCounts(payload.deltaCounts);
    if (payload.sessionRestarted) viewer.needsSnapshot = true; // new session: counts/lines start over
    if (payload.sessionRestarted || payload.more) logRefreshQueued = true;
  } else {
    viewer.setEntries(payload.entries, { before: payload.before, detached: payload.detached });
    if (payload.counts) viewer.setCounts(payload.counts);
    // A filter with few matches in this session leaves nothing to scroll, so keep searching
    // earlier lines until the console fills up or the start of the log is reached.
    const el = viewer.preEl;
    if (el && el.scrollHeight - el.clientHeight < LOG_LOAD_OLDER_PX) void loadOlderLogs(viewer);
  }
}

async function loadOlderLogs(viewer) {
  if (!viewer.canLoadOlder()) return;
  const generation = viewer.generation;
  const filterKey = viewer.filterKey;
  viewer.loadingOlder = true;
  viewer._syncTopMarker();
  try {
    const params = new URLSearchParams({
      stream: viewer.name,
      format: "json",
      level: viewer.level,
      q: viewer.query,
      limit: String(LOG_PAGE_LINES),
      before: String(viewer.before),
    });
    const payload = await fetchJson(`/api/admin/logs?${params.toString()}`);
    if (viewer.generation !== generation || viewer.filterKey !== filterKey || payload?.format !== "jsonl") return;
    viewer.loadingOlder = false;
    viewer.prependEntries(payload.entries, payload.before);
  } catch (e) {
    if (viewer.generation === generation) toast(adminEndpointErrorMessage(e, "Logs"), { kind: "error" });
  } finally {
    if (viewer.generation === generation) {
      viewer.loadingOlder = false;
      viewer._syncTopMarker();
      // Still at the top (e.g. a page with few filter matches): keep going.
      if (viewer.canLoadOlder() && viewer.preEl.scrollTop < LOG_LOAD_OLDER_PX) {
        requestAnimationFrame(() => void loadOlderLogs(viewer));
      }
    }
  }
}

// Next refresh loads a fresh snapshot (latest lines + counts) instead of catching up from where
// the viewer left off.
function requestLogSnapshot(viewers = [serverLogViewer, pollerLogViewer]) {
  viewers.forEach((v) => {
    if (v) v.needsSnapshot = true;
  });
  requestLogRefresh();
}

async function refreshLogs() {
  if (!isPaneLive("logs") || logsPaused) return;
  if (logRefreshInFlight) {
    logRefreshQueued = true;
    return;
  }
  logRefreshInFlight = true;
  const hint = document.getElementById("adminAuthHint");
  try {
    await Promise.all(visibleLogViewers().map((v) => refreshLogStream(v)));
    if (hint?.textContent.startsWith("Logs")) hint.textContent = "";
  } catch (e) {
    if (hint) hint.textContent = adminEndpointErrorMessage(e, "Logs");
  } finally {
    logRefreshInFlight = false;
  }
  if (logRefreshQueued) {
    logRefreshQueued = false;
    void refreshLogs();
  }
}

function requestLogRefresh() {
  void refreshLogs();
}

function setupLogsWindow() {
  const panesEl = document.getElementById("adminLogPanes");
  if (!panesEl) return;
  serverLogViewer = new LogViewer({
    name: "server",
    preEl: document.getElementById("serverConsole"),
    toolbarEl: document.getElementById("serverConsoleToolbar"),
    paneEl: document.getElementById("serverConsolePane"),
    onFilterChange: requestLogRefresh,
  });
  pollerLogViewer = new LogViewer({
    name: "poller",
    preEl: document.getElementById("pollerConsole"),
    toolbarEl: document.getElementById("pollerConsoleToolbar"),
    paneEl: document.getElementById("pollerConsolePane"),
    onFilterChange: requestLogRefresh,
  });
  serverLogViewer.onNeedOlder = loadOlderLogs;
  pollerLogViewer.onNeedOlder = loadOlderLogs;

  const viewButtons = Array.from(document.querySelectorAll("[data-log-view]"));
  const setView = (view) => {
    const v = ["server", "poller", "split"].includes(view) ? view : "split";
    panesEl.dataset.view = v;
    viewButtons.forEach((b) => b.setAttribute("aria-selected", b.dataset.logView === v ? "true" : "false"));
    try {
      window.localStorage.setItem(LOG_VIEW_STORAGE_KEY, v);
    } catch {
      // storage unavailable
    }
    requestLogSnapshot(visibleLogViewers());
    requestAnimationFrame(() => visibleLogViewers().forEach((lv) => lv.follow && lv.scrollToBottom()));
  };
  viewButtons.forEach((b) => b.addEventListener("click", () => setView(b.dataset.logView)));

  const followEl = document.getElementById("adminLogFollow");
  const setFollow = (on) => {
    [serverLogViewer, pollerLogViewer].forEach((v) => {
      v.follow = on;
      if (on) v.scrollToBottom();
    });
    if (followEl) followEl.checked = on;
    try {
      window.localStorage.setItem(LOG_FOLLOW_STORAGE_KEY, on ? "1" : "0");
    } catch {
      // storage unavailable
    }
  };
  followEl?.addEventListener("change", () => setFollow(followEl.checked));

  // Pause stops fetching new lines (to read or copy); resuming catches up from where it left off.
  const pauseEl = document.getElementById("adminLogPause");
  pauseEl?.addEventListener("change", () => {
    logsPaused = pauseEl.checked;
    document.getElementById("adminLogWindow")?.classList.toggle("is-paused", logsPaused);
    if (!logsPaused) requestLogRefresh();
  });

  let savedView = "split";
  let savedFollow = true;
  try {
    savedView = window.localStorage.getItem(LOG_VIEW_STORAGE_KEY) || "split";
    savedFollow = window.localStorage.getItem(LOG_FOLLOW_STORAGE_KEY) !== "0";
  } catch {
    // storage unavailable
  }
  setFollow(savedFollow);
  setView(savedView);
}

async function refreshVisitors() {
  if (isEditingAdminDatalist()) {
    return;
  }
  try {
    const data = await fetchJson("/api/admin/visitor-map");
    renderVisitorMap(data);
    renderVisitorTable(data);
    const hint = document.getElementById("adminAuthHint");
    if (hint && !hint.textContent.startsWith("Logs:")) hint.textContent = "";
  } catch (e) {
    const hint = document.getElementById("adminAuthHint");
    if (hint) {
      hint.textContent = adminEndpointErrorMessage(e, "Visitors");
    }
  }
}

function formatBytesMb(valueMb) {
  if (!Number.isFinite(valueMb)) return "—";
  if (valueMb >= 1024) return `${(valueMb / 1024).toFixed(2)} GB`;
  if (valueMb >= 100) return `${valueMb.toFixed(0)} MB`;
  return `${valueMb.toFixed(1)} MB`;
}

function formatPercent(value) {
  if (!Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}

function formatUptimeFromBootMs(bootTimeMs) {
  if (!Number.isFinite(bootTimeMs)) return "—";
  const diffMs = Date.now() - bootTimeMs;
  if (!Number.isFinite(diffMs) || diffMs < 0) return "—";
  const s = Math.floor(diffMs / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatDurationFromMs(durationMs) {
  if (!Number.isFinite(durationMs)) return "—";
  if (durationMs < 0) return "—";
  const s = Math.floor(durationMs / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatSinceDeployFromDeployTimeMs(deployTimeMs) {
  if (!Number.isFinite(deployTimeMs)) return "—";
  return formatDurationFromMs(Date.now() - deployTimeMs);
}

function setStatsHint(text, isWarn = false) {
  const hint = document.getElementById("adminStatsHint");
  if (!hint) return;
  hint.textContent = text || "";
  hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
}

function renderStatsCards(payload) {
  const grid = document.getElementById("adminStatsGrid");
  if (!grid) return;

  const cpu = payload?.system?.cpu || {};
  const mem = payload?.system?.memory || {};
  const swap = payload?.system?.swap || {};
  const net = payload?.system?.net || {};

  // Percent tiles get a meter: green below 60%, orange below 85%, red above.
  const meter = (pct) => {
    if (!Number.isFinite(pct)) return "";
    const level = pct >= 85 ? "high" : pct >= 60 ? "mid" : "low";
    const width = Math.max(2, Math.min(100, pct));
    return `<span class="mac-meter" data-level="${level}"><i style="width:${width.toFixed(1)}%"></i></span>`;
  };
  const cards = [
    { key: "cpu", k: "CPU", v: formatPercent(cpu.percent), sub: "Processor load", pct: cpu.percent },
    {
      key: "ram",
      k: "Memory",
      v: formatPercent(mem.usedPercent),
      sub: `${formatBytesMb(mem.usedMb)} of ${formatBytesMb(mem.totalMb)}`,
      pct: mem.usedPercent,
    },
    {
      key: "swap",
      k: "Swap",
      v: formatPercent(swap.usedPercent),
      sub: `${formatBytesMb(swap.usedMb)} of ${formatBytesMb(swap.totalMb)}`,
      pct: swap.usedPercent,
    },
    { key: "uptime", k: "Uptime", v: formatUptimeFromBootMs(payload?.system?.bootTimeMs), sub: "Since the server booted" },
    { key: "sinceDeploy", k: "Last deploy", v: formatSinceDeployFromDeployTimeMs(payload?.app?.deployTimeMs), sub: "Time since the app started" },
    { key: "net", k: "Network", v: `${formatBytesMb(net.rxMb)} ↓`, sub: `${formatBytesMb(net.txMb)} ↑ sent` },
  ];

  grid.innerHTML = cards
    .map(
      (c) => `
    <div class="admin-stats-card admin-stats-card--${escapeHtml(c.key)}">
      <p class="admin-stats-k">${escapeHtml(c.k)}</p>
      <p class="admin-stats-v">${escapeHtml(c.v)}</p>
      <p class="admin-stats-sub">${escapeHtml(c.sub || "")}</p>
      ${meter(c.pct)}
    </div>`,
    )
    .join("");
}

async function refreshStats() {
  if (isEditingAdminDatalist()) {
    return;
  }
  try {
    const payload = await fetchJson("/api/admin/stats");
    if (!payload?.ok) {
      setStatsHint(payload?.error ? `Stats: ${payload.error}` : "Stats: unavailable", true);
      return;
    }
    setStatsHint("");
    renderStatsCards(payload);
  } catch (e) {
    setStatsHint(adminEndpointErrorMessage(e, "Stats"), true);
  }
}

function setupCsvDownload() {
  const a = document.getElementById("csvDownloadBtn");
  if (!a) return;
  a.addEventListener("click", (ev) => {
    ev.preventDefault();
    window.alert(
      "CSV export has been removed.\n\nMarket data is stored in SQLite (data/market.db).",
    );
  });
}

function setupDbDownload() {
  const btn = document.getElementById("downloadDbBtn");
  const hint = document.getElementById("adminDataHint");
  if (!btn) return;

  const setHint = (text, isWarn = false) => {
    if (!hint) return;
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  btn.addEventListener("click", () => {
    setHint("Preparing download…");
    // Navigation-based download so cookies/auth work and the browser handles the save dialog.
    window.location.assign("/api/admin/download/market.db");
    window.setTimeout(() => setHint(""), 2000);
  });
}

function setupMlRetrain() {
  const btn = document.getElementById("mlRetrainTriggerBtn");
  const hint = document.getElementById("mlRetrainHint");
  const statusEl = document.getElementById("mlRetrainStatus");
  if (!btn || !statusEl) return;

  const setHint = (text, isWarn = false) => {
    if (!hint) return;
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  const fmtPerDay = (v) => (typeof v === "number" ? `${v > 0 ? "+" : ""}${v.toFixed(3)}%/day` : "—");

  const renderStatus = (status, model) => {
    if (!status || Object.keys(status).length === 0) {
      statusEl.innerHTML = '<p class="admin-muted" style="margin:0">No retrain has run yet.</p>';
      return;
    }
    const perDay = model?.returnPerDayPct || {};
    const modelRows = model
      ? [
          ["Ranking", model.enabled ? "Learned model" : "Estimator formula"],
          ["Model gate", model.enabled ? "Passed" : model.disabledReason || "—"],
          ["Backtest estimator", fmtPerDay(perDay.estimator)],
          ["Backtest random", fmtPerDay(perDay.random)],
          ["Model vs estimator", `${fmtPerDay(perDay.modelOnActiveWeeks)} vs ${fmtPerDay(perDay.estimatorOnActiveWeeks)} (${model.data?.modelActiveWeeks ?? 0} weeks)`],
        ]
      : [];
    const rows = [
      ...modelRows,
      ["Status", status.last_status ?? "—"],
      ["Running", status.running ? "Yes" : "No"],
      ["Last run week", status.last_run_week_key || "—"],
      ["Last attempt", formatWhen(status.last_attempt_at_utc)],
      ["Last completed", formatWhen(status.last_completed_at_utc)],
      ["Exit code", status.last_exit_code != null ? String(status.last_exit_code) : "—"],
      [
        "Log file",
        status.last_log_path
          ? { text: String(status.last_log_path).split(/[\\/]/).pop(), title: String(status.last_log_path) }
          : "—",
      ],
    ];
    // A value is text, or { text, title } where title is shown on hover (exact time, full path).
    const rowsHtml = rows
      .map(([k, v]) => {
        const text = typeof v === "object" && v ? v.text : v;
        const title = typeof v === "object" && v?.title ? ` title="${escapeHtml(v.title)}"` : "";
        return `<div class="admin-appconfig-row"><span class="admin-appconfig-k">${escapeHtml(k)}</span><span class="admin-appconfig-v"${title}>${escapeHtml(text)}</span></div>`;
      })
      .join("");
    statusEl.innerHTML = rowsHtml;
    if (status.last_log_tail) {
      statusEl.innerHTML += `<details style="margin-top:10px"><summary class="admin-muted">Log tail</summary><pre class="admin-console" style="margin-top:6px;max-height:200px;overflow:auto">${escapeHtml(status.last_log_tail)}</pre></details>`;
    }
  };

  // Load status on page load.
  fetchJson("/api/admin/ml-retrain-status")
    .then((d) => renderStatus(d?.status ?? {}, d?.model))
    .catch((e) => {
      statusEl.innerHTML = `<p class="admin-muted" style="margin:0;color:var(--warn)">${adminEndpointErrorMessage(e, "ML retrain status")}</p>`;
    });

  btn.addEventListener("click", async () => {
    const ok = await confirmSheet({
      title: "Retrain the ranking now?",
      message: "The poller starts the retrain on its next cycle (within ~30 seconds). It runs in the background and takes a few minutes.",
      confirmLabel: "Retrain",
    });
    if (!ok) return;

    btn.disabled = true;
    setHint("Triggering…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/trigger-ml-retrain", { method: "POST" });
      if (!payload?.ok) {
        setHint(payload?.error ? `Trigger failed: ${payload.error}` : "Trigger failed.", true);
        toast(payload?.error ? `Retrain failed: ${payload.error}` : "Retrain could not be started.", { kind: "error" });
        return;
      }
      setHint("Triggered. The poller will start the retrain on its next cycle.");
      toast("Retrain scheduled for the next poller cycle.");
      // Refresh status after a short delay.
      setTimeout(() => {
        fetchJson("/api/admin/ml-retrain-status")
          .then((d) => renderStatus(d?.status ?? {}, d?.model))
          .catch(() => {});
      }, 3000);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "ML retrain trigger"), true);
      toast(adminEndpointErrorMessage(e, "ML retrain trigger"), { kind: "error" });
    } finally {
      btn.disabled = false;
    }
  });
}

function setupCompanionTrackRecord() {
  const summaryEl = document.getElementById("companionTrackRecord");
  const tbody = document.querySelector("#companionPicksTable tbody");
  if (!summaryEl || !tbody) return;

  const pct = (v, digits = 0) => (typeof v === "number" ? `${(v * 100).toFixed(digits)}%` : "—");
  const signedPct = (v, digits = 1) => (typeof v === "number" ? `${v > 0 ? "+" : ""}${v.toFixed(digits)}%` : "—");
  const mirrors = (v) => (typeof v === "number" ? (v >= 10 ? v.toFixed(0) : v.toFixed(2)) : "—");

  const blockRow = (label, b) => {
    if (!b || !b.picks) return [label, "No finished picks yet"];
    return [
      label,
      `${b.picks} picks · sold ${pct(b.actualSellRate)} (predicted ${pct(b.predictedSellRate)}) · ` +
        `return ${signedPct(b.actualReturnPct)} (predicted ${signedPct(b.predictedReturnPct)}) · ` +
        `${typeof b.actualReturnPerDayPct === "number" ? `${signedPct(b.actualReturnPerDayPct, 3)}/day` : "—"}`,
    ];
  };

  const render = (d) => {
    const rows = [
      ["Logged picks", `${d.logged ?? 0} (${d.pending ?? 0} still within their horizon)`],
      blockRow("All finished picks", d.evaluated),
      blockRow("Top 5 picks", d.top5),
    ];
    summaryEl.innerHTML = rows
      .map(([k, v]) => `<div class="admin-appconfig-row"><span class="admin-appconfig-k">${escapeHtml(k)}</span><span class="admin-appconfig-v">${escapeHtml(v)}</span></div>`)
      .join("");

    const picks = Array.isArray(d.recent) ? d.recent : [];
    if (!picks.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="admin-muted">No picks logged yet. They appear after the companion is used.</td></tr>';
      return;
    }
    tbody.innerHTML = picks
      .map((p) => {
        const ask = p.askWholeMirrors ? `${p.askWholeMirrors} mirror${p.askWholeMirrors === 1 ? "" : "s"}` : mirrors(p.askPriceMirror);
        const outcome =
          p.status === "pending"
            ? "Pending"
            : p.status === "sold"
              ? `Sold in ${p.daysToSell ?? "?"}d (${signedPct(p.returnPct)})`
              : `Not sold (${signedPct(p.returnPct)})`;
        return `<tr><td>${escapeHtml(p.week)}</td><td>${escapeHtml(p.itemName)}</td><td>${escapeHtml(String(p.bestRank))}</td>` +
          `<td>${escapeHtml(`${mirrors(p.entryPriceMirror)} → ${ask}`)}</td>` +
          `<td>${escapeHtml(`${pct(p.sellProbability)} · ~${Math.round(p.expectedDays)}d`)}</td><td>${escapeHtml(outcome)}</td></tr>`;
      })
      .join("");
  };

  fetchJson("/api/admin/companion/track-record")
    .then(render)
    .catch((e) => {
      summaryEl.innerHTML = `<p class="admin-muted" style="margin:0;color:var(--warn)">${adminEndpointErrorMessage(e, "Companion track record")}</p>`;
    });
}

function setupRunDbExport() {
  const btn = document.getElementById("runDbExportBtn");
  const hint = document.getElementById("adminDataHint");
  if (!btn) return;

  const setHint = (text, isWarn = false) => {
    if (!hint) return;
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  btn.addEventListener("click", async () => {
    const ok = await confirmSheet({
      title: "Export the database now?",
      message: "A snapshot of market.db is uploaded to the backup Discord channel.",
      confirmLabel: "Export",
    });
    if (!ok) return;

    btn.disabled = true;
    setHint("Running DB export…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/run-db-export", { method: "POST" });
      if (!payload?.ok) {
        setHint(payload?.error ? `DB export: ${payload.error}` : "DB export failed.", true);
        toast(payload?.error ? `DB export failed: ${payload.error}` : "DB export failed.", { kind: "error" });
        return;
      }
      const name = payload?.file || "export";
      const sizeMiB = payload?.sizeMiB;
      const done = sizeMiB != null ? `DB export uploaded: ${name} (${sizeMiB} MiB).` : `DB export uploaded: ${name}.`;
      setHint(done);
      toast(done);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "DB export"), true);
      toast(adminEndpointErrorMessage(e, "DB export"), { kind: "error" });
    } finally {
      btn.disabled = false;
    }
  });
}

function setupClearData() {
  const btn = document.getElementById("clearDataBtn");
  const hint = document.getElementById("adminDataHint");
  if (!btn) return;

  const setHint = (text, isWarn = false) => {
    if (!hint) return;
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  btn.addEventListener("click", async () => {
    const ok = await confirmSheet({
      title: "Clear all market data?",
      message: "This deletes every poll, listing snapshot and recorded sale from the database. It cannot be undone.",
      confirmLabel: "Clear data",
      destructive: true,
      requireText: "clear",
    });
    if (!ok) return;

    btn.disabled = true;
    setHint("Clearing data…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/clear-data", { method: "POST" });
      const cleared = payload?.cleared || {};
      const sqlite = cleared.sqlite ? "sqlite" : null;
      setHint(sqlite ? "Cleared: sqlite" : "Cleared.");
      toast("Market data cleared.");
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Clear data"), true);
      toast(adminEndpointErrorMessage(e, "Clear data"), { kind: "error" });
    } finally {
      btn.disabled = false;
    }
  });
}

function setupAlertTestTool() {
  const itemSelect = document.getElementById("adminAlertTestItemSelect");
  const variantSelect = document.getElementById("adminAlertTestVariantSelect");
  const salesCb = document.getElementById("adminAlertTypeSales");
  const snipeCb = document.getElementById("adminAlertTypeSnipe");
  const repriceCb = document.getElementById("adminAlertTypeReprice");
  const newItemsCb = document.getElementById("adminAlertTypeNewItems");
  const preview = document.getElementById("adminAlertTestPreview");
  const sendBtn = document.getElementById("adminAlertTestSendBtn");
  const hint = document.getElementById("adminAlertTestHint");
  if (!itemSelect || !variantSelect || !salesCb || !snipeCb || !repriceCb || !newItemsCb || !preview || !sendBtn || !hint) return;

  const setHint = (text, isWarn = false) => {
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  let variants = [];
  let variantsByItem = new Map();

  const selectedVariant = () => {
    const vid = Number(variantSelect.value || 0);
    if (!Number.isFinite(vid) || vid <= 0) return null;
    return variants.find((v) => Number(v?.variantId || 0) === vid) || null;
  };

  const selectedTypes = () => {
    const out = [];
    if (salesCb.checked) out.push("sales");
    if (snipeCb.checked) out.push("snipe");
    if (repriceCb.checked) out.push("reprice");
    if (newItemsCb.checked) out.push("new_items");
    return out;
  };

  const renderPreview = () => {
    const v = selectedVariant();
    const types = selectedTypes();
    if (!v) {
      preview.textContent = "Select item and variant.";
      sendBtn.disabled = true;
      return;
    }
    const mode = v.mode ? `mode=${v.mode}` : "mode=any";
    const filter = v.imageNameFilter ? `filter=${v.imageNameFilter}` : "filter=(none)";
    const salesCount = Number(v.salesCount || 0);
    const typesText = types.length ? types.join(", ") : "(select at least one type)";
    preview.textContent = `${v.displayName} · ${mode} · ${filter} · recorded sales=${salesCount} · send: ${typesText}`;
    sendBtn.disabled = types.length === 0;
  };

  const populateVariantsForItem = () => {
    const item = String(itemSelect.value || "").trim();
    const rows = variantsByItem.get(item) || [];
    variantSelect.innerHTML = "";
    if (!rows.length) {
      variantSelect.disabled = true;
      variantSelect.innerHTML = '<option value="">No variants for selected item</option>';
      renderPreview();
      return;
    }
    variantSelect.disabled = false;
    rows.forEach((v, idx) => {
      const opt = document.createElement("option");
      opt.value = String(v.variantId);
      const mode = v.mode ? v.mode : "any";
      const filter = v.imageNameFilter ? ` · ${v.imageNameFilter}` : "";
      opt.textContent = `${v.displayName} (${mode}${filter})`;
      variantSelect.appendChild(opt);
      if (idx === 0) opt.selected = true;
    });
    renderPreview();
  };

  itemSelect.addEventListener("change", populateVariantsForItem);
  variantSelect.addEventListener("change", renderPreview);
  salesCb.addEventListener("change", renderPreview);
  snipeCb.addEventListener("change", renderPreview);
  repriceCb.addEventListener("change", renderPreview);
  newItemsCb.addEventListener("change", renderPreview);

  sendBtn.addEventListener("click", async () => {
    const v = selectedVariant();
    if (!v) return;
    const types = selectedTypes();
    if (!types.length) {
      setHint("Select at least one alert type.", true);
      return;
    }
    sendBtn.disabled = true;
    setHint("Sending test alerts…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/alerts/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ variantId: v.variantId, types }),
      });
      if (!payload?.ok) {
        setHint(payload?.error ? `Alert test: ${payload.error}` : "Alert test failed.", true);
        toast(payload?.error ? `Alert test: ${payload.error}` : "Alert test failed.", { kind: "error" });
        return;
      }
      const sent = Array.isArray(payload?.sent) ? payload.sent.join(", ") : "none";
      const skipped = Array.isArray(payload?.skipped) && payload.skipped.length ? ` · skipped: ${payload.skipped.join("; ")}` : "";
      setHint(`Sent: ${sent}${skipped}`);
      toast(`Test alerts sent: ${sent}${skipped}`);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Alert test"), true);
      toast(adminEndpointErrorMessage(e, "Alert test"), { kind: "error" });
    } finally {
      renderPreview();
    }
  });

  async function loadVariants() {
    itemSelect.disabled = true;
    variantSelect.disabled = true;
    itemSelect.innerHTML = '<option value="">Loading items…</option>';
    variantSelect.innerHTML = '<option value="">Loading variants…</option>';
    setHint("");
    try {
      const payload = await fetchJson("/api/admin/market/variants-sales");
      if (!payload?.ok) throw new Error(payload?.error || "Failed to load variants");
      variants = Array.isArray(payload?.variants) ? payload.variants : [];
      variantsByItem = new Map();
      variants.forEach((v) => {
        const item = String(v?.baseItemName || "").trim();
        if (!item) return;
        if (!variantsByItem.has(item)) variantsByItem.set(item, []);
        variantsByItem.get(item).push(v);
      });

      itemSelect.innerHTML = "";
      const names = Array.from(variantsByItem.keys()).sort((a, b) => a.localeCompare(b));
      if (!names.length) {
        itemSelect.disabled = true;
        itemSelect.innerHTML = '<option value="">No tracked items found</option>';
        variantSelect.disabled = true;
        variantSelect.innerHTML = '<option value="">No variants available</option>';
        renderPreview();
        return;
      }
      names.forEach((name, idx) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        if (idx === 0) opt.selected = true;
        itemSelect.appendChild(opt);
      });
      itemSelect.disabled = false;
      populateVariantsForItem();
    } catch (e) {
      itemSelect.disabled = true;
      variantSelect.disabled = true;
      itemSelect.innerHTML = '<option value="">Failed to load items</option>';
      variantSelect.innerHTML = '<option value="">Failed to load variants</option>';
      setHint(adminEndpointErrorMessage(e, "Load alert-test variants"), true);
      renderPreview();
    }
  }

  void loadVariants();
}

function setupDeleteSalesTool() {
  const itemSelect = document.getElementById("adminSalesItemSelect");
  const variantSelect = document.getElementById("adminSalesVariantSelect");
  const preview = document.getElementById("adminSalesPreview");
  const saleSelect = document.getElementById("adminSalesEventSelect");
  const salePreview = document.getElementById("adminSalesEventPreview");
  const historyBtn = document.getElementById("adminWipePriceHistoryBtn");
  const btn = document.getElementById("adminWipeVariantBtn");
  const resendBtn = document.getElementById("adminResendSaleAlertBtn");
  const hint = document.getElementById("adminSalesDeleteHint");
  if (!itemSelect || !variantSelect || !preview || !saleSelect || !salePreview || !historyBtn || !btn || !resendBtn || !hint) return;

  const setHint = (text, isWarn = false) => {
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  let variants = [];
  let variantsByItem = new Map();
  let selected = null;
  let sales = [];
  let saleGroups = [];

  const variantLabel = (v) => {
    const mode = String(v?.mode || "").trim() || "normal";
    const image = String(v?.imageNameFilter || "").trim() || "default";
    const salesCount = Number(v?.salesCount) || 0;
    return `${mode} · image: ${image} · sales: ${salesCount} · #${v.variantId}`;
  };

  const fmt = (v) => {
    const mode = v.mode ? ` · ${v.mode}` : "";
    const image = v.imageNameFilter ? ` · image: ${v.imageNameFilter}` : "";
    const base = v.baseItemName ? ` · base: ${v.baseItemName}` : "";
    return `${v.displayName}${mode}${image}${base}`;
  };

  const updatePreview = () => {
    const v = selected;
    if (!v) {
      preview.textContent = "Select an item variant.";
      btn.disabled = true;
      historyBtn.disabled = true;
      resendBtn.disabled = true;
      saleSelect.disabled = true;
      saleSelect.innerHTML = `<option value="">Select a variant first…</option>`;
      salePreview.textContent = "Select a sale to resend.";
      return;
    }
    const count = Number(v.salesCount) || 0;
    const label = fmt(v);
    preview.textContent = `${label} · recorded sales: ${count}`;
    btn.disabled = false;
    historyBtn.disabled = false;
  };

  const saleRuleLabel = (rule) => {
    const r = String(rule || "");
    if (r === "confirmed_transfer") return "Transfer";
    if (r === "likely_instant_sale") return "Likely instant";
    if (r === "likely_non_instant_online_sale") return "Likely non-instant online";
    return r || "Unknown";
  };

  const salePriceLabel = (s) => {
    const amount = Number(s?.priceAmount);
    const cur = String(s?.priceCurrency || "").trim();
    if (Number.isFinite(amount) && cur) return `${amount} ${cur}`;
    const m = Number(s?.mirrorEquiv);
    if (Number.isFinite(m)) return `${m} mirrors`;
    return "price n/a";
  };

  const formatWhen = (iso) => {
    if (!iso) return "unknown time";
    const d = new Date(String(iso));
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  };

  const localMinuteBucket = (iso) => {
    const d = new Date(String(iso || ""));
    if (Number.isNaN(d.getTime())) {
      return {
        key: `unknown-${String(iso || "")}`,
        label: `unknown (${String(iso || "") || "n/a"})`,
        sortTs: Number.NEGATIVE_INFINITY,
      };
    }
    const y = d.getFullYear();
    const M = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const key = `${y}-${M}-${dd} ${hh}:${mm}`;
    const label = `${dd}-${M}-${y} ${hh}:${mm}`;
    const sortTs = new Date(y, d.getMonth(), d.getDate(), d.getHours(), d.getMinutes(), 0, 0).getTime();
    return { key, label, sortTs };
  };

  const buildSaleGroups = (rows) => {
    const map = new Map();
    for (const s of Array.isArray(rows) ? rows : []) {
      const bucket = localMinuteBucket(s?.occurredAtUtc);
      if (!map.has(bucket.key)) {
        map.set(bucket.key, {
          key: bucket.key,
          label: bucket.label,
          sortTs: bucket.sortTs,
          entries: [],
          saleIds: [],
          signals: 0,
        });
      }
      const group = map.get(bucket.key);
      group.entries.push(s);
      const sid = Number(s?.saleId);
      if (Number.isFinite(sid) && sid > 0) group.saleIds.push(sid);
      const qty = Number(s?.quantity);
      group.signals += Number.isFinite(qty) && qty > 0 ? Math.floor(qty) : 1;
    }
    const out = Array.from(map.values());
    out.forEach((g) => {
      const uniq = new Set();
      g.saleIds = g.saleIds.filter((n) => !uniq.has(n) && uniq.add(n));
      g.entries.sort((a, b) => String(b?.occurredAtUtc || "").localeCompare(String(a?.occurredAtUtc || "")));
    });
    out.sort((a, b) => Number(b.sortTs || 0) - Number(a.sortTs || 0));
    return out;
  };

  const selectedGroup = () => {
    const key = String(saleSelect.value || "").trim();
    if (!key) return null;
    return saleGroups.find((g) => g.key === key) || null;
  };

  const updateSalePreview = () => {
    const g = selectedGroup();
    if (!g) {
      resendBtn.disabled = true;
      salePreview.textContent = sales.length ? "Select a grouped timestamp to resend." : "No recorded sales for this variant.";
      return;
    }
    resendBtn.disabled = false;
    const sellers = new Set();
    for (const s of g.entries) {
      if (s?.seller) sellers.add(String(s.seller));
    }
    const sellersPreview = Array.from(sellers).slice(0, 3).join(", ");
    const sellersMore = sellers.size > 3 ? ` +${sellers.size - 3} more` : "";
    salePreview.textContent = `${g.label} · ${g.signals} sale signal(s) · sellers: ${sellersPreview || "unknown"}${sellersMore}`;
  };

  const renderSalesSelect = () => {
    saleSelect.innerHTML = "";
    if (!selected) {
      saleSelect.disabled = true;
      saleSelect.innerHTML = `<option value="">Select a variant first…</option>`;
      updateSalePreview();
      return;
    }
    if (!sales.length) {
      saleSelect.disabled = true;
      saleSelect.innerHTML = `<option value="">No recorded sales</option>`;
      updateSalePreview();
      return;
    }
    saleGroups = buildSaleGroups(sales);
    saleSelect.disabled = false;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a timestamp group (date + HH:mm)…";
    saleSelect.appendChild(placeholder);
    saleGroups.forEach((g) => {
      const opt = document.createElement("option");
      opt.value = g.key;
      opt.textContent = `${g.label} · ${g.signals} sale signal(s)`;
      saleSelect.appendChild(opt);
    });
    saleSelect.value = "";
    updateSalePreview();
    // Reset delete button label since no group is selected after a (re)render.
    if (btn) btn.textContent = "Delete all sales + fingerprints";
  };

  const loadSalesForSelected = async () => {
    if (!selected) {
      sales = [];
      renderSalesSelect();
      return;
    }
    salePreview.textContent = "Loading sales…";
    resendBtn.disabled = true;
    try {
      const payload = await fetchJson(`/api/admin/market/sales?variantId=${encodeURIComponent(selected.variantId)}&limit=200`);
      if (!payload?.ok) throw new Error(payload?.error || "Failed to load sales");
      sales = Array.isArray(payload.sales) ? payload.sales : [];
      saleGroups = [];
      renderSalesSelect();
    } catch (e) {
      sales = [];
      saleGroups = [];
      renderSalesSelect();
      salePreview.textContent = adminEndpointErrorMessage(e, "Load sales");
    }
  };

  const applySelectedVariant = () => {
    const raw = String(variantSelect.value || "").trim();
    const id = Number(raw);
    selected = Number.isFinite(id) && id > 0 ? variants.find((v) => Number(v.variantId) === id) || null : null;
    updatePreview();
    void loadSalesForSelected();
  };

  const populateVariantSelect = (preferredVariantId) => {
    const item = String(itemSelect.value || "").trim();
    const rows = variantsByItem.get(item) || [];
    variantSelect.innerHTML = "";
    if (!rows.length) {
      variantSelect.disabled = true;
      variantSelect.innerHTML = '<option value="">No variants for selected item</option>';
      selected = null;
      updatePreview();
      void loadSalesForSelected();
      return;
    }

    const normalized = rows
      .slice()
      .sort((a, b) => variantLabel(a).localeCompare(variantLabel(b)));
    normalized.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = String(v.variantId);
      opt.textContent = variantLabel(v);
      variantSelect.appendChild(opt);
    });
    variantSelect.disabled = false;

    const preferred = String(preferredVariantId || "").trim();
    if (preferred && normalized.some((v) => String(v.variantId) === preferred)) {
      variantSelect.value = preferred;
    } else {
      variantSelect.value = String(normalized[0].variantId);
    }
    applySelectedVariant();
  };

  const populateItemSelect = (preferredItem, preferredVariantId) => {
    itemSelect.innerHTML = "";
    const names = Array.from(variantsByItem.keys()).sort((a, b) => a.localeCompare(b));
    if (!names.length) {
      itemSelect.disabled = true;
      itemSelect.innerHTML = '<option value="">No tracked items found</option>';
      variantSelect.disabled = true;
      variantSelect.innerHTML = '<option value="">No variants available</option>';
      selected = null;
      updatePreview();
      void loadSalesForSelected();
      return;
    }
    names.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      const count = (variantsByItem.get(name) || []).length;
      opt.textContent = `${name} (${count})`;
      itemSelect.appendChild(opt);
    });
    itemSelect.disabled = false;

    if (preferredItem && variantsByItem.has(preferredItem)) {
      itemSelect.value = preferredItem;
    } else {
      itemSelect.value = names[0];
    }
    populateVariantSelect(preferredVariantId);
  };

  itemSelect.addEventListener("change", () => {
    populateVariantSelect(null);
  });

  variantSelect.addEventListener("change", () => {
    applySelectedVariant();
  });

  const updateDeleteBtnLabel = () => {
    const g = selectedGroup();
    btn.textContent = g ? "Delete selected sale" : "Delete all sales + fingerprints";
  };

  const deleteSelectedVariantData = async (requestBody, successMessage, errorLabel) => {
    if (!selected) return;
    btn.disabled = true;
    historyBtn.disabled = true;
    setHint("Deleting…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/market/wipe-variant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      if (!payload?.ok) {
        setHint(payload?.error ? `${errorLabel}: ${payload.error}` : `${errorLabel} failed`, true);
        toast(payload?.error ? `${errorLabel}: ${payload.error}` : `${errorLabel} failed`, { kind: "error" });
        return;
      }
      successMessage(payload);
      toast(document.getElementById("adminSalesDeleteHint")?.textContent || "Deleted.");
      const keepItem = String(itemSelect.value || "").trim();
      const keepVariantId = selected ? Number(selected.variantId) : null;
      await loadVariants();
      populateItemSelect(keepItem, keepVariantId);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, errorLabel), true);
      toast(adminEndpointErrorMessage(e, errorLabel), { kind: "error" });
    } finally {
      btn.disabled = false;
      historyBtn.disabled = false;
    }
  };

  historyBtn.addEventListener("click", async () => {
    if (!selected) return;
    const ok = await confirmSheet({
      title: `Delete all price history for ${selected.displayName}?`,
      message:
        "Every poll for this art variant is removed, with its listing snapshots, inference events, sales and fingerprint state. It cannot be undone.",
      confirmLabel: "Delete history",
      destructive: true,
      requireText: "delete",
    });
    if (!ok) return;
    await deleteSelectedVariantData(
      { scope: "history", variantId: selected.variantId },
      (payload) => {
        const pollsN = payload?.deleted?.priceHistoryPolls ?? 0;
        const salesN = payload?.deleted?.sales ?? 0;
        const listingsN = payload?.deleted?.listingSnapshots ?? 0;
        const eventsN = payload?.deleted?.inferenceEvents ?? 0;
        const pendingN = payload?.deleted?.inferencePending ?? 0;
        const signalsN = payload?.deleted?.inferenceSignals ?? 0;
        setHint(
          `Deleted price history ${pollsN}, sales ${salesN}, listings ${listingsN}, events ${eventsN}, pending ${pendingN}, signals ${signalsN}.`,
        );
      },
      "Delete price history",
    );
  });

  saleSelect.addEventListener("change", () => {
    updateSalePreview();
    updateDeleteBtnLabel();
  });

  resendBtn.addEventListener("click", async () => {
    const g = selectedGroup();
    if (!g || !selected) return;
    resendBtn.disabled = true;
    setHint("Resending alert…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/sales/resend-alert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ saleIds: g.saleIds }),
      });
      if (!payload?.ok) {
        setHint(payload?.error ? `Resend sale alert: ${payload.error}` : "Resend sale alert failed.", true);
        toast(payload?.error ? `Resend sale alert: ${payload.error}` : "Resend sale alert failed.", { kind: "error" });
        return;
      }
      setHint(`Resent sale alert for ${payload?.item || selected.displayName} (${g.label}, ${g.signals} signal(s)).`);
      toast(`Sale alert resent for ${payload?.item || selected.displayName}.`);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Resend sale alert"), true);
      toast(adminEndpointErrorMessage(e, "Resend sale alert"), { kind: "error" });
    } finally {
      updateSalePreview();
    }
  });

  btn.addEventListener("click", async () => {
    if (!selected) return;
    const g = selectedGroup();
    let msg;
    let requestBody;
    if (g) {
      msg =
        `${selected.displayName} · ${g.label} · ${g.saleIds.length} sale record(s).\n\n` +
        "Fingerprints and inference state are not affected. It cannot be undone.";
      requestBody = { scope: "sales", variantId: selected.variantId, saleIds: g.saleIds };
    } else {
      msg =
        "Removes all recorded sales, the listing and inference fingerprints, and the est. sold counters for this variant. " +
        "It cannot be undone.";
      requestBody = { scope: "variant", variantId: selected.variantId };
    }
    const ok = await confirmSheet({
      title: g ? "Delete this sale?" : `Delete all sales for ${selected.displayName}?`,
      message: msg,
      confirmLabel: g ? "Delete sale" : "Delete all",
      destructive: true,
      requireText: g ? "" : "delete",
    });
    if (!ok) return;
    await deleteSelectedVariantData(
      requestBody,
      (payload) => {
        if (g) {
          const salesN = payload?.deleted?.sales ?? 0;
          setHint(`Deleted ${salesN} sale record(s) for ${selected.displayName} (${g.label}).`);
          return;
        }
        const salesN = payload?.deleted?.sales ?? payload?.deletedSales ?? 0;
        const listingsN = payload?.deleted?.listingSnapshots ?? 0;
        const eventsN = payload?.deleted?.inferenceEvents ?? 0;
        const pendingN = payload?.deleted?.inferencePending ?? 0;
        const signalsN = payload?.deleted?.inferenceSignals ?? 0;
        const pollsN = payload?.updated?.pollsReset ?? payload?.pollsUpdated ?? 0;
        setHint(
          `Deleted sales ${salesN}, listings ${listingsN}, events ${eventsN}, pending ${pendingN}, signals ${signalsN}. Reset polls ${pollsN}.`,
        );
      },
      "Delete item data",
    );
  });

  async function loadVariants() {
    preview.textContent = "Loading…";
    itemSelect.disabled = true;
    variantSelect.disabled = true;
    itemSelect.innerHTML = '<option value="">Loading items…</option>';
    variantSelect.innerHTML = '<option value="">Loading variants…</option>';
    btn.disabled = true;
    resendBtn.disabled = true;
    setHint("");
    try {
      const payload = await fetchJson("/api/admin/market/variants-sales");
      if (!payload?.ok) throw new Error(payload?.error || "Failed to load variants");
      variants = Array.isArray(payload?.variants) ? payload.variants : [];
      variantsByItem = new Map();
      variants.forEach((v) => {
        const item = String(v?.baseItemName || "").trim();
        if (!item) return;
        if (!variantsByItem.has(item)) variantsByItem.set(item, []);
        variantsByItem.get(item).push(v);
      });

      const keepItem = selected ? String(selected.baseItemName || "").trim() : null;
      const keepVariantId = selected ? Number(selected.variantId) : null;
      populateItemSelect(keepItem, keepVariantId);
    } catch (e) {
      itemSelect.disabled = true;
      variantSelect.disabled = true;
      itemSelect.innerHTML = '<option value="">Failed to load items</option>';
      variantSelect.innerHTML = '<option value="">Failed to load variants</option>';
      preview.textContent = adminEndpointErrorMessage(e, "Load variants");
      sales = [];
      saleGroups = [];
      renderSalesSelect();
    }
  }

  void loadVariants();
}

function setupMarketConfigEditor() {
  const KEY_DEFAULT = "market";
  const JSON_EDITOR_OPEN_STORAGE_KEY = "poe-admin-json-editor-open";
  const jsonEl = document.getElementById("marketCfgJson");
  const prettyEl = document.getElementById("marketCfgJsonPretty");
  const editorEl = jsonEl?.closest(".admin-json-editor");
  const detailsEl = jsonEl?.closest(".admin-marketcfg-advanced");
  const keyEl = document.getElementById("marketCfgKey");
  const saveBtn = document.getElementById("marketCfgSaveBtn");
  const fmtBtn = document.getElementById("marketCfgFormatBtn");
  const reloadBtn = document.getElementById("marketCfgReloadBtn");
  const hintEl = document.getElementById("marketCfgHint");
  if (!jsonEl || !prettyEl || !editorEl || !saveBtn || !fmtBtn || !reloadBtn || !hintEl) return;

  const MIN_JSON_EDITOR_HEIGHT_PX = 120;
  const fitJsonEditorToContent = () => {
    jsonEl.style.height = "auto";
    const targetHeight = Math.max(MIN_JSON_EDITOR_HEIGHT_PX, jsonEl.scrollHeight);
    const px = `${targetHeight}px`;
    editorEl.style.height = px;
    prettyEl.style.height = px;
    jsonEl.style.height = px;
  };

  const setHint = (text, isWarn = false) => {
    hintEl.textContent = text || "";
    hintEl.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  const formatConfigTimestamp = (value) => (value ? formatWhen(value).text : "");

  const escapeHtml = (s) =>
    String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const highlightJson = (raw) => {
    const text = String(raw ?? "");
    // Tokenizer: strings, numbers, true/false/null, punctuation
    const re =
      /("(?:\\.|[^"\\])*")|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|([{}\[\]:,])/g;
    let out = "";
    let last = 0;
    let m;
    while ((m = re.exec(text))) {
      out += escapeHtml(text.slice(last, m.index));
      if (m[1]) {
        // string: detect key by looking ahead for colon
        const str = m[1];
        const isKey = (() => {
          const after = text.slice(m.index + str.length);
          return /^\s*:/.test(after);
        })();
        out += `<span class="${isKey ? "admin-json-k" : "admin-json-s"}">${escapeHtml(str)}</span>`;
      } else if (m[2]) {
        out += `<span class="admin-json-n">${escapeHtml(m[2])}</span>`;
      } else if (m[3]) {
        const cls = m[3] === "null" ? "admin-json-null" : "admin-json-b";
        out += `<span class="${cls}">${escapeHtml(m[3])}</span>`;
      } else if (m[4]) {
        out += `<span class="admin-json-p">${escapeHtml(m[4])}</span>`;
      }
      last = m.index + m[0].length;
    }
    out += escapeHtml(text.slice(last));
    // keep final line height stable
    if (out.endsWith("\n") || text.endsWith("\n")) out += "<br/>";
    return out;
  };

  let hlTimer = null;
  const syncHighlight = () => {
    if (hlTimer) window.clearTimeout(hlTimer);
    hlTimer = window.setTimeout(() => {
      fitJsonEditorToContent();
      prettyEl.innerHTML = highlightJson(jsonEl.value || "");
      // sync scroll
      prettyEl.scrollTop = jsonEl.scrollTop;
      prettyEl.scrollLeft = jsonEl.scrollLeft;
    }, 20);
  };

  const syncScroll = () => {
    prettyEl.scrollTop = jsonEl.scrollTop;
    prettyEl.scrollLeft = jsonEl.scrollLeft;
  };

  const getSelectedKey = () => {
    const raw = String(keyEl?.value || "").trim();
    return raw || KEY_DEFAULT;
  };

  const restoreJsonEditorOpenState = () => {
    if (!detailsEl) return;
    try {
      const raw = window.localStorage.getItem(JSON_EDITOR_OPEN_STORAGE_KEY);
      if (raw === "1" || raw === "0") {
        detailsEl.open = raw === "1";
      }
    } catch {
      // ignore localStorage errors (private mode / blocked storage)
    }
  };

  const persistJsonEditorOpenState = () => {
    if (!detailsEl) return;
    try {
      window.localStorage.setItem(JSON_EDITOR_OPEN_STORAGE_KEY, detailsEl.open ? "1" : "0");
    } catch {
      // ignore localStorage errors (private mode / blocked storage)
    }
  };

  // Unsaved-changes tracking and live validation.
  let savedValue = "";
  let loadedKey = getSelectedKey();
  const isDirty = () => jsonEl.value !== savedValue;
  const jsonErrorAt = (raw) => {
    // Browsers word JSON errors differently (Chrome often gives no position), so find the
    // first offending character ourselves with a small validating scanner.
    let i = 0;
    const fail = (what) => {
      throw { index: i, what };
    };
    const ws = () => {
      while (i < raw.length && " \t\r\n".includes(raw[i])) i += 1;
    };
    const lit = (word) => {
      if (raw.startsWith(word, i)) i += word.length;
      else fail("unexpected value");
    };
    const str = () => {
      i += 1;
      while (i < raw.length && raw[i] !== '"') {
        if (raw[i] === "\\") i += 1;
        else if (raw[i] === "\n") fail("line break inside a string");
        i += 1;
      }
      if (raw[i] !== '"') fail("unterminated string");
      i += 1;
    };
    const num = () => {
      const m = raw.slice(i).match(/^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/);
      if (!m || !m[0]) fail("expected a value");
      i += m[0].length;
    };
    const value = () => {
      ws();
      const c = raw[i];
      if (c === "{") {
        i += 1;
        ws();
        if (raw[i] === "}") {
          i += 1;
          return;
        }
        for (;;) {
          ws();
          if (raw[i] !== '"') fail(raw[i] === "}" ? "trailing comma" : "expected a quoted key");
          str();
          ws();
          if (raw[i] !== ":") fail("expected ':' after the key");
          i += 1;
          value();
          ws();
          if (raw[i] === ",") {
            i += 1;
            continue;
          }
          if (raw[i] === "}") {
            i += 1;
            return;
          }
          fail("expected ',' or '}'");
        }
      } else if (c === "[") {
        i += 1;
        ws();
        if (raw[i] === "]") {
          i += 1;
          return;
        }
        for (;;) {
          value();
          ws();
          if (raw[i] === ",") {
            i += 1;
            ws();
            if (raw[i] === "]") fail("trailing comma");
            continue;
          }
          if (raw[i] === "]") {
            i += 1;
            return;
          }
          fail("expected ',' or ']'");
        }
      } else if (c === '"') str();
      else if (c === "t") lit("true");
      else if (c === "f") lit("false");
      else if (c === "n") lit("null");
      else if (c === undefined) fail("unexpected end of input");
      else num();
    };
    try {
      value();
      ws();
      if (i < raw.length) fail("unexpected text after the end");
      return null;
    } catch (e) {
      const before = raw.slice(0, e.index);
      const lines = before.split("\n");
      return { line: lines.length, col: lines[lines.length - 1].length + 1, msg: e.what };
    }
  };
  const syncDirtyState = () => {
    const dirty = isDirty();
    saveBtn.classList.toggle("is-dirty", dirty);
    saveBtn.title = dirty ? "Save changes (Ctrl/⌘+S)" : "No unsaved changes";
  };
  const validate = () => {
    const raw = (jsonEl.value || "").trim();
    let error = null;
    if (raw) {
      try {
        JSON.parse(raw);
      } catch (e) {
        error = jsonErrorAt(jsonEl.value) || { line: null, col: null, msg: String(e?.message || e) };
      }
    }
    editorEl.classList.toggle("is-invalid", !!error);
    saveBtn.disabled = !!error || !raw;
    if (error) {
      const where = error.line ? `Line ${error.line}, column ${error.col}: ` : "";
      setHint(`Invalid JSON — ${where}${error.msg}`, true);
    } else if (isDirty()) {
      setHint("Unsaved changes.");
    }
    syncDirtyState();
    return !error;
  };
  const confirmDiscard = () =>
    !isDirty() ||
    confirmSheet({
      title: "Discard unsaved changes?",
      message: `Your edits to "${loadedKey}" haven't been saved.`,
      confirmLabel: "Discard",
      destructive: true,
    });
  window.addEventListener("beforeunload", (ev) => {
    if (!isDirty()) return;
    ev.preventDefault();
    ev.returnValue = "";
  });

  const loadKey = async () => {
    const key = getSelectedKey();
    setHint("Loading…");
    reloadBtn.disabled = true;
    try {
      const payload = await fetchJson(`/api/admin/app-config/get?key=${encodeURIComponent(key)}`);
      if (!payload?.ok) {
        setHint(payload?.error || "Not found.", true);
        return;
      }
      jsonEl.value = payload?.value_json || "";
      savedValue = jsonEl.value;
      loadedKey = key;
      syncHighlight();
      validate();
      setHint(
        payload?.updated_at_utc
          ? `Loaded ${key} · updated ${formatConfigTimestamp(payload.updated_at_utc)}`
          : `Loaded ${key}.`,
      );
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, `Load ${key} config`), true);
    } finally {
      reloadBtn.disabled = false;
    }
  };

  const formatJson = () => {
    const raw = (jsonEl.value || "").trim();
    if (!raw) {
      setHint("Nothing to format.", true);
      return;
    }
    try {
      const parsed = JSON.parse(raw);
      jsonEl.value = JSON.stringify(parsed, null, 2);
      syncHighlight();
      fitJsonEditorToContent();
      validate();
      setHint(isDirty() ? "Formatted · unsaved changes." : "Formatted.");
    } catch {
      validate();
    }
  };

  const saveKey = async () => {
    const key = getSelectedKey();
    const raw = (jsonEl.value || "").trim();
    if (!raw) {
      setHint("Value is empty.", true);
      return;
    }
    // Validate client-side before sending.
    if (!validate()) return;
    setHint("Saving…");
    saveBtn.disabled = true;
    try {
      const payload = await fetchJsonWithInit("/api/admin/app-config/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, value_json: raw }),
      });
      if (!payload?.ok) {
        setHint(payload?.error || "Save failed.", true);
        toast(payload?.error ? `Save failed: ${payload.error}` : "Save failed.", { kind: "error" });
        return;
      }
      // Server returns normalized JSON.
      if (typeof payload.value_json === "string") {
        jsonEl.value = payload.value_json;
        syncHighlight();
      }
      savedValue = jsonEl.value;
      setHint(
        payload?.updated_at_utc
          ? `Saved ${key} · updated ${formatConfigTimestamp(payload.updated_at_utc)}`
          : `Saved ${key}.`,
      );
      toast(`Saved ${key} config. It applies on the next poller cycle.`);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, `Save ${key} config`), true);
      toast(adminEndpointErrorMessage(e, `Save ${key} config`), { kind: "error" });
    } finally {
      saveBtn.disabled = false;
      syncDirtyState();
    }
  };

  saveBtn.addEventListener("click", () => void saveKey());
  fmtBtn.addEventListener("click", () => formatJson());
  reloadBtn.addEventListener("click", async () => {
    if (await confirmDiscard()) void loadKey();
  });
  keyEl?.addEventListener("change", async () => {
    if (await confirmDiscard()) {
      void loadKey();
    } else {
      keyEl.value = loadedKey; // stay on the key with the unsaved edits
    }
  });
  detailsEl?.addEventListener("toggle", persistJsonEditorOpenState);

  let validateTimer = null;
  jsonEl.addEventListener("input", () => {
    fitJsonEditorToContent();
    syncHighlight();
    syncDirtyState();
    window.clearTimeout(validateTimer);
    validateTimer = window.setTimeout(validate, 250);
  });

  jsonEl.addEventListener("scroll", syncScroll);

  // Keyboard helpers.
  jsonEl.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") {
      ev.preventDefault();
      void saveKey();
    }
  });

  // Initial paint
  restoreJsonEditorOpenState();
  fitJsonEditorToContent();
  syncHighlight();
  void loadKey();
  document.addEventListener("admin:pane-shown", (ev) => {
    if (ev.detail === "config") fitJsonEditorToContent();
  });
}

function setupRestartPoller() {
  const btn = document.getElementById("restartPollerBtn");
  const hint = document.getElementById("adminPollerHint");
  if (!btn) return;

  const setHint = (text, isWarn = false) => {
    if (!hint) return;
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  btn.addEventListener("click", async () => {
    const ok = await confirmSheet({
      title: "Restart the poller?",
      message: "The running poller (if any) is stopped and a new one is started by the server.",
      confirmLabel: "Restart",
    });
    if (!ok) return;

    btn.disabled = true;
    setHint("Restarting poller…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/restart-poller", { method: "POST" });
      const pid = payload?.managed?.start?.pid ?? payload?.start?.pid ?? payload?.pid;
      setHint(pid ? `Poller restarted (pid ${pid}).` : "Poller restart triggered.");
      toast(pid ? `Poller restarted (pid ${pid}).` : "Poller restart triggered.");
      pollerLogViewer?.reset();
      window.setTimeout(() => void refreshPollerStatus(), 5000); // the new poller clears a pending deploy flag
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Restart poller"), true);
      toast(adminEndpointErrorMessage(e, "Restart poller"), { kind: "error" });
    } finally {
      btn.disabled = false;
    }
  });
}

// Deploys defer the poller restart to the end of its current cycle; show when one is waiting.
async function refreshPollerStatus() {
  const badge = document.getElementById("pollerRestartBadge");
  if (!badge) return;
  try {
    const payload = await fetchJson("/api/admin/poller-status");
    const pending = Boolean(payload?.restart_pending);
    badge.hidden = !pending;
    if (!pending) return;
    const commit = payload.deployed_commit ? String(payload.deployed_commit) : "";
    badge.textContent = commit ? `Restart pending · ${commit}` : "Restart pending";
    const when = formatWhen(payload.requested_at_utc);
    badge.title = `Deploy${commit ? ` ${commit}` : ""} requested ${when.text}. The poller restarts after its current cycle.`;
  } catch {
    // Keep the last known state; the stats hint already reports admin endpoint errors.
  }
}

function setupStopPoller() {
  const btn = document.getElementById("stopPollerBtn");
  const hint = document.getElementById("adminPollerHint");
  if (!btn) return;

  const setHint = (text, isWarn = false) => {
    if (!hint) return;
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  btn.addEventListener("click", async () => {
    const ok = await confirmSheet({
      title: "Stop the poller?",
      message: "Polling stops until you restart it. Prices, sales and alerts won't update meanwhile.",
      confirmLabel: "Stop poller",
      destructive: true,
    });
    if (!ok) return;

    btn.disabled = true;
    setHint("Stopping poller…");
    try {
      await fetchJsonWithInit("/api/admin/stop-poller", { method: "POST" });
      setHint("Poller stopped.");
      toast("Poller stopped.", { kind: "info" });
      appendLocalConsoleLine(pollerLogViewer, { msg: "[admin] Poller stopped." });
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Stop poller"), true);
      toast(adminEndpointErrorMessage(e, "Stop poller"), { kind: "error" });
    } finally {
      btn.disabled = false;
    }
  });
}

function setupMapResize() {
  let t;
  const bump = () => {
    if (!map) return;
    clearTimeout(t);
    t = window.setTimeout(() => {
      map.invalidateSize({ animate: false });
    }, 120);
  };
  window.addEventListener("resize", bump);
  window.addEventListener("orientationchange", bump);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", bump);
  }
}

function main() {
  setupMacWindow({
    onMinimizedChange: (minimized) => {
      windowMinimized = minimized;
      if (!minimized && activePane) onPaneShown(activePane); // catch up on what changed
    },
  });
  setupCsvDownload();
  setupDbDownload();
  setupRunDbExport();
  setupClearData();
  setupAlertTestTool();
  setupDeleteSalesTool();
  setupMarketConfigEditor();
  setupMlRetrain();
  setupCompanionTrackRecord();
  setupStopPoller();
  setupRestartPoller();
  setupMapResize();
  setupLogsWindow();
  setupPanes();
  window.setInterval(refreshLogs, LOG_POLL_MS);
  window.setInterval(() => isPaneLive("overview") && void refreshStats(), 10000);
  window.setInterval(() => isPaneLive("overview") && void refreshPollerStatus(), 15000);
  window.setInterval(() => isPaneLive("visitors") && void refreshVisitors(), 60000);
}

main();
