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

const fetchOpts = { credentials: "same-origin" };

async function fetchJson(path) {
  const res = await fetch(path, fetchOpts);
  if (res.status === 403) {
    const err = new Error("Forbidden");
    err.status = 403;
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
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

let map;
let heatLayer;
let markersLayer;

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
  if (heatLayer) {
    map.removeLayer(heatLayer);
    heatLayer = null;
  }
  markersLayer.clearLayers();

  const heat = points.map((p) => [p.lat, p.lng, Math.max(0.15, Math.min(1, p.weight / 8))]);
  if (heat.length > 0 && typeof L.heatLayer === "function") {
    heatLayer = L.heatLayer(heat, {
      radius: 32,
      blur: 28,
      maxZoom: 12,
      max: 1.2,
      gradient: { 0.4: "blue", 0.65: "lime", 0.85: "yellow", 1: "red" },
    });
    map.addLayer(heatLayer);
  }

  points.forEach((p) => {
    const m = L.circleMarker([p.lat, p.lng], {
      radius: 6 + Math.min(10, p.visits / 2),
      color: "#2ab7bf",
      weight: 2,
      fillColor: "#ff7a2f",
      fillOpacity: 0.6,
    });
    const lastSeen = p.lastSeen ? formatLocalDateTime(p.lastSeen) : "";
    m.bindPopup(
      `<strong>${escapeHtml(p.ip)}</strong><br/>Visits: ${p.visits}<br/>${lastSeen ? escapeHtml(lastSeen) : ""}`,
    );
    markersLayer.addLayer(m);
  });

  if (points.length > 0) {
    const bounds = L.latLngBounds(points.map((p) => [p.lat, p.lng]));
    map.fitBounds(bounds.pad(0.15));
  }

  requestAnimationFrame(() => {
    map.invalidateSize({ animate: false });
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderVisitorTable(data) {
  const tbody = document.getElementById("visitorTableBody");
  const stats = document.getElementById("visitorStats");
  if (!tbody) return;

  const visitors = data.visitors || [];
  const pending = data.pendingGeocodes ?? 0;
  if (stats) {
    stats.textContent = `${data.uniqueVisitors ?? 0} unique IPs · ${data.totalVisits ?? 0} page loads (dashboard)`;
    if (pending > 0) {
      stats.textContent += ` · ${pending} IP(s) not yet placed on map (refresh to load more; rate-limited geocoding)`;
    }
  }

  tbody.innerHTML = visitors
    .map(
      (v) => `
    <tr>
      <td><code>${escapeHtml(v.ip)}</code></td>
      <td>${v.visits}</td>
      <td>${v.lastSeen ? escapeHtml(formatLocalDateTime(v.lastSeen)) : "—"}</td>
      <td>${v.onMap ? "Yes" : "—"}</td>
    </tr>`,
    )
    .join("");
}

let logPollTimer;
let mapPollTimer;

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function setupLogSplitView() {
  const splitEl = document.getElementById("adminLogSplit");
  const handle = document.getElementById("adminLogSplitHandle");
  const leftPane = document.getElementById("serverConsolePane");
  const rightPane = document.getElementById("pollerConsolePane");
  if (!splitEl || !handle || !leftPane || !rightPane) return;

  const storageKey = "admin.logSplit.ratio";
  const minRatio = 0.22;
  const maxRatio = 0.78;

  const applyRatio = (ratio) => {
    const r = clamp(Number(ratio) || 0.5, minRatio, maxRatio);
    leftPane.style.flex = `${r} 1 0`;
    rightPane.style.flex = `${1 - r} 1 0`;
    handle.setAttribute("aria-valuenow", String(Math.round(r * 100)));
    handle.setAttribute("aria-valuemin", String(Math.round(minRatio * 100)));
    handle.setAttribute("aria-valuemax", String(Math.round(maxRatio * 100)));
  };

  const saved = Number.parseFloat(window.localStorage.getItem(storageKey) || "");
  applyRatio(Number.isFinite(saved) ? saved : 0.5);

  let dragging = false;
  let activePointerId = null;

  const ratioFromClientX = (clientX) => {
    const rect = splitEl.getBoundingClientRect();
    const x = clamp(clientX - rect.left, 0, rect.width);
    return clamp(x / rect.width, minRatio, maxRatio);
  };

  const onPointerMove = (ev) => {
    if (!dragging) return;
    const ratio = ratioFromClientX(ev.clientX);
    applyRatio(ratio);
    window.localStorage.setItem(storageKey, String(ratio));
  };

  const stopDrag = () => {
    if (!dragging) return;
    dragging = false;
    handle.dataset.dragging = "false";
    if (activePointerId != null) {
      handle.releasePointerCapture?.(activePointerId);
    }
    activePointerId = null;
  };

  handle.addEventListener("pointerdown", (ev) => {
    // No split drag in stacked (mobile) layout.
    if (window.matchMedia?.("(max-width: 768px)")?.matches) return;
    dragging = true;
    handle.dataset.dragging = "true";
    activePointerId = ev.pointerId;
    handle.setPointerCapture?.(ev.pointerId);
    ev.preventDefault();
  });

  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", stopDrag);
  window.addEventListener("pointercancel", stopDrag);

  handle.addEventListener("keydown", (ev) => {
    // Keyboard accessibility: arrow keys resize by 2%.
    const step = 0.02;
    const currentLeft = leftPane.getBoundingClientRect().width;
    const currentTotal = splitEl.getBoundingClientRect().width;
    if (currentTotal <= 0) return;
    let ratio = currentLeft / currentTotal;
    if (ev.key === "ArrowLeft") ratio -= step;
    else if (ev.key === "ArrowRight") ratio += step;
    else return;
    ratio = clamp(ratio, minRatio, maxRatio);
    applyRatio(ratio);
    window.localStorage.setItem(storageKey, String(ratio));
    ev.preventDefault();
  });
}

class LogViewer {
  constructor({ preEl, toolbarEl, onChange }) {
    this.preEl = preEl;
    this.toolbarEl = toolbarEl;
    this.onChange = typeof onChange === "function" ? onChange : null;

    this.entries = [];
    this.level = "all"; // all | info | warn | error
    this.query = "";
    this.cursor = 0;
    this._filterKey = "";

    this.pills = {};
    this._setupToolbar();
  }

  _setupToolbar() {
    if (!this.toolbarEl) return;
    this.toolbarEl.innerHTML = "";

    const makePill = (key, label, extraClass = "") => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `admin-pill ${extraClass}`.trim();
      btn.setAttribute("aria-pressed", "false");
      btn.addEventListener("click", () => {
        if (key.startsWith("level:")) {
          this.level = key.slice("level:".length);
          this._syncPressed();
          this._filterKey = "";
          this.onChange?.();
          return;
        }
      });
      this.toolbarEl.appendChild(btn);
      this.pills[key] = btn;
      btn.textContent = label;
      return btn;
    };

    makePill("level:all", "All");
    makePill("level:info", "Info");
    makePill("level:warn", "Warning", "admin-pill--warn");
    makePill("level:error", "Error", "admin-pill--error");

    const input = document.createElement("input");
    input.className = "admin-pill-input";
    input.type = "search";
    input.placeholder = "Filter… (text match)";
    input.addEventListener("input", () => {
      this.query = input.value || "";
      this._filterKey = "";
      this.onChange?.();
    });
    this.toolbarEl.appendChild(input);

    this._syncPressed();
  }

  _syncPressed() {
    const pressed = (key, val) => {
      const el = this.pills[key];
      if (!el) return;
      el.setAttribute("aria-pressed", val ? "true" : "false");
    };

    pressed("level:all", this.level === "all");
    pressed("level:info", this.level === "info");
    pressed("level:warn", this.level === "warn");
    pressed("level:error", this.level === "error");
  }

  setEntries({ entries, counts }) {
    this.entries = Array.isArray(entries) ? entries : [];
    const c = counts && typeof counts === "object" ? counts : null;
    if (c) {
      const all = Number.isFinite(c.all) ? c.all : this.entries.length;
      const info = Number.isFinite(c.info) ? c.info : 0;
      const warning = Number.isFinite(c.warning) ? c.warning : 0;
      const error = Number.isFinite(c.error) ? c.error : 0;
      this._setCounts({ all, info, warning, error });
    } else {
      this._setCounts({ all: this.entries.length, info: 0, warning: 0, error: 0 });
    }
    this.render();
  }

  appendEntries({ entries }) {
    const next = Array.isArray(entries) ? entries : [];
    if (!next.length) return;
    this.entries = this.entries.concat(next);
    if (this.entries.length > 2000) {
      this.entries = this.entries.slice(this.entries.length - 2000);
    }
    this.render();
  }

  _setCounts({ all, info, warning, error }) {
    const setLabel = (key, label) => {
      const el = this.pills[key];
      if (el) el.textContent = label;
    };
    setLabel("level:all", `All (${all})`);
    setLabel("level:info", `Info (${info})`);
    setLabel("level:warn", `Warning (${warning})`);
    setLabel("level:error", `Error (${error})`);
  }

  render() {
    if (!this.preEl) return;

    const wasNearBottom = this.preEl.scrollHeight - (this.preEl.scrollTop + this.preEl.clientHeight) < 24;

    let html = "";
    for (const entry of this.entries) {
      const levelRaw = String(entry?.level || "info").toLowerCase();
      const level = levelRaw === "warning" ? "warn" : (levelRaw === "error" ? "error" : "info");
      const tsRaw = entry?.ts ? String(entry.ts) : "";
      let ts = "";
      if (tsRaw) {
        const d = new Date(tsRaw);
        ts = Number.isNaN(d.getTime())
          ? tsRaw
          : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      }
      const msg = entry?.msg ? String(entry.msg) : "";
      const exc = entry?.exc ? String(entry.exc) : "";
      const line = level === "error" ? [ts, msg, exc].filter(Boolean).join(" ") : [ts, msg].filter(Boolean).join(" ");
      const printable = line.length ? line : "(blank)";
      html += `<span class="log-line log-line--${level}">${escapeHtml(printable)}</span>\n`;
    }

    if (!this.entries.length) {
      html = `<span class="log-line log-line--muted">(no matching lines)</span>\n`;
    }

    // Render.
    this.preEl.innerHTML = html;

    if (wasNearBottom) {
      this.preEl.scrollTop = this.preEl.scrollHeight;
    }
  }
}

let serverLogViewer;
let pollerLogViewer;
let logRefreshTick = 0;

async function refreshLogs() {
  const serverEl = document.getElementById("serverConsole");
  const pollerEl = document.getElementById("pollerConsole");
  try {
    if (serverEl) {
      if (!serverLogViewer) {
        serverLogViewer = new LogViewer({
          preEl: serverEl,
          toolbarEl: document.getElementById("serverConsoleToolbar"),
          onChange: () => void refreshLogs(),
        });
      }
    }
    if (pollerEl) {
      if (!pollerLogViewer) {
        pollerLogViewer = new LogViewer({
          preEl: pollerEl,
          toolbarEl: document.getElementById("pollerConsoleToolbar"),
          onChange: () => void refreshLogs(),
        });
      }
    }

    logRefreshTick += 1;

    const buildRequest = (stream, viewer) => {
      const level = viewer?.level || "all";
      const q = viewer?.query || "";
      const filterKey = `${level}::${q}`;
      const filterChanged = !!viewer && viewer._filterKey !== filterKey;
      const doCounts = filterChanged || logRefreshTick % 8 === 1; // refresh counts periodically
      return {
        params: new URLSearchParams({
          stream,
          format: "json",
          since: "session",
          level,
          q,
          limit: "2000",
          ...(filterChanged ? {} : { cursor: String(viewer?.cursor || 0) }),
          counts: doCounts ? "1" : "0",
        }),
        filterKey,
        doCounts,
        filterChanged,
      };
    };

    const serverReq = buildRequest("server", serverLogViewer);
    const pollerReq = buildRequest("poller", pollerLogViewer);

    const [serverPayload, pollerPayload] = await Promise.all([
      fetchJson(`/api/admin/logs?${serverReq.params.toString()}`),
      fetchJson(`/api/admin/logs?${pollerReq.params.toString()}`),
    ]);

    if (serverLogViewer && serverPayload?.format === "jsonl") {
      serverLogViewer.cursor = Number.isFinite(serverPayload.cursor) ? serverPayload.cursor : (serverLogViewer.cursor || 0);
      serverLogViewer._filterKey = serverReq.filterKey;
      if (serverPayload.delta) {
        serverLogViewer.appendEntries({ entries: serverPayload.entries });
        if (serverReq.doCounts && serverPayload.counts) {
          serverLogViewer.setEntries({ entries: serverLogViewer.entries, counts: serverPayload.counts });
        }
      } else {
        serverLogViewer.setEntries({ entries: serverPayload.entries, counts: serverPayload.counts });
      }
    }
    if (pollerLogViewer && pollerPayload?.format === "jsonl") {
      pollerLogViewer.cursor = Number.isFinite(pollerPayload.cursor) ? pollerPayload.cursor : (pollerLogViewer.cursor || 0);
      pollerLogViewer._filterKey = pollerReq.filterKey;
      if (pollerPayload.delta) {
        pollerLogViewer.appendEntries({ entries: pollerPayload.entries });
        if (pollerReq.doCounts && pollerPayload.counts) {
          pollerLogViewer.setEntries({ entries: pollerLogViewer.entries, counts: pollerPayload.counts });
        }
      } else {
        pollerLogViewer.setEntries({ entries: pollerPayload.entries, counts: pollerPayload.counts });
      }
    }

    const hint = document.getElementById("adminAuthHint");
    if (hint) hint.textContent = "";
  } catch (e) {
    const hint = document.getElementById("adminAuthHint");
    if (hint) {
      hint.textContent =
        e.status === 403
          ? "Unauthorized. Open /admin?token=… once using the ADMIN_TOKEN from the server (GitHub Actions secret), then this page will set a cookie."
          : `Logs: ${e.message || e}`;
    }
  }
}

async function refreshVisitors() {
  try {
    const data = await fetchJson("/api/admin/visitor-map");
    renderVisitorMap(data);
    renderVisitorTable(data);
    const hint = document.getElementById("adminAuthHint");
    if (hint && !hint.textContent.startsWith("Logs:")) hint.textContent = "";
  } catch (e) {
    const hint = document.getElementById("adminAuthHint");
    if (hint) {
      hint.textContent =
        e.status === 403
          ? "Unauthorized. Open /admin?token=… once using the ADMIN_TOKEN from the server (GitHub Actions secret), then this page will set a cookie."
          : `Visitors: ${e.message || e}`;
    }
  }
}

function setupCsvDownload() {
  const a = document.getElementById("csvDownloadBtn");
  if (!a) return;
  a.addEventListener("click", (ev) => {
    ev.preventDefault();
    window.location.href = "/api/admin/download/price_poll.csv";
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
  setupCsvDownload();
  setupMapResize();
  setupLogSplitView();
  refreshLogs();
  refreshVisitors();
  logPollTimer = window.setInterval(refreshLogs, 2500);
  mapPollTimer = window.setInterval(refreshVisitors, 60000);
}

main();
