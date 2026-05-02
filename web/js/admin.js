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

let map;
let heatLayer;
let markersLayer;
let visitorMarkersByIp = {};
let lastVisitorMapData = null;
let userFocusedVisitor = false;
const visitorTableSort = { key: "visits", direction: "desc" };

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
    visitorMarkersByIp[String(p.ip || "")] = m;
  });

  // Default view: auto-fit all known points (unless user manually focused a visitor).
  if (!userFocusedVisitor) {
    if (points.length >= 2) {
      const bounds = markersLayer.getBounds?.();
      if (bounds && bounds.isValid?.()) {
        map.fitBounds(bounds, {
          padding: [28, 28],
          animate: false,
          maxZoom: 6,
        });
      } else {
        map.setView([20, 0], 2, { animate: false });
      }
    } else if (points.length === 1) {
      const p = points[0];
      if (p && Number.isFinite(p.lat) && Number.isFinite(p.lng)) {
        map.setView([p.lat, p.lng], 6, { animate: false });
      } else {
        map.setView([20, 0], 2, { animate: false });
      }
    } else {
      map.setView([20, 0], 2, { animate: false });
    }
  }

  requestAnimationFrame(() => {
    map.invalidateSize({ animate: false });
  });
}

function focusVisitorIp(ip) {
  ensureMap();
  const key = String(ip || "");
  if (!key) return;
  const marker = visitorMarkersByIp[key];
  if (marker) {
    userFocusedVisitor = true;
    const ll = marker.getLatLng();
    map.setView(ll, Math.max(map.getZoom(), 6), { animate: true });
    marker.openPopup();
    return;
  }
  // If an IP exists in the table but isn't placed yet (no geo), do nothing.
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function compareVisitorRows(a, b) {
  const direction = visitorTableSort.direction === "asc" ? 1 : -1;
  const key = visitorTableSort.key;

  if (key === "visits") {
    const av = Number(a?.visits) || 0;
    const bv = Number(b?.visits) || 0;
    if (av !== bv) return (av - bv) * direction;
  } else if (key === "lastSeen") {
    const at = a?.lastSeen ? Date.parse(String(a.lastSeen)) : Number.NEGATIVE_INFINITY;
    const bt = b?.lastSeen ? Date.parse(String(b.lastSeen)) : Number.NEGATIVE_INFINITY;
    if (at !== bt) return (at - bt) * direction;
  } else {
    const cmp = String(a?.ip || "").localeCompare(String(b?.ip || ""), undefined, { numeric: true, sensitivity: "base" });
    if (cmp !== 0) return cmp * direction;
  }

  const ipCmp = String(a?.ip || "").localeCompare(String(b?.ip || ""), undefined, { numeric: true, sensitivity: "base" });
  if (ipCmp !== 0) return ipCmp;
  return (Number(a?.visits) || 0) - (Number(b?.visits) || 0);
}

function updateVisitorTableSortUi() {
  document.querySelectorAll?.("#visitorTable thead th").forEach((th) => {
    const btn = th.querySelector?.("[data-visitor-sort-key]");
    const key = btn?.getAttribute("data-visitor-sort-key") || "";
    const active = key === visitorTableSort.key;
    const direction = active ? visitorTableSort.direction : "none";
    th.setAttribute("aria-sort", direction === "asc" ? "ascending" : (direction === "desc" ? "descending" : "none"));
    btn?.classList.toggle("is-active", active);
    btn?.setAttribute(
      "aria-label",
      active
        ? `${btn.textContent} sorted ${direction === "asc" ? "ascending" : "descending"}. Activate to sort ${direction === "asc" ? "descending" : "ascending"}.`
        : `${btn.textContent} not sorted. Activate to sort ascending.`,
    );
  });
}

function setupVisitorTableSorting() {
  document.querySelectorAll?.("[data-visitor-sort-key]").forEach((btn) => {
    if (btn.dataset.sortReady === "1") return;
    btn.dataset.sortReady = "1";
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-visitor-sort-key");
      if (!key) return;
      if (visitorTableSort.key === key) {
        visitorTableSort.direction = visitorTableSort.direction === "asc" ? "desc" : "asc";
      } else {
        visitorTableSort.key = key;
        visitorTableSort.direction = key === "visits" || key === "lastSeen" ? "desc" : "asc";
      }
      updateVisitorTableSortUi();
      if (lastVisitorMapData) renderVisitorTable(lastVisitorMapData);
    });
  });
  updateVisitorTableSortUi();
}

function renderVisitorTable(data) {
  const tbody = document.getElementById("visitorTableBody");
  const stats = document.getElementById("visitorStats");
  if (!tbody) return;

  setupVisitorTableSorting();

  const visitors = [...(data.visitors || [])].sort(compareVisitorRows);
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
    <tr class="visitor-row" data-ip="${escapeHtml(v.ip)}">
      <td><code>${escapeHtml(v.ip)}</code></td>
      <td>${v.visits}</td>
      <td>${v.lastSeen ? escapeHtml(formatLocalDateTime(v.lastSeen)) : "—"}</td>
    </tr>`,
    )
    .join("");

  // Click a row to focus the map marker.
  tbody.querySelectorAll?.("tr.visitor-row").forEach((tr) => {
    tr.addEventListener("click", () => {
      const ip = tr.getAttribute("data-ip") || "";
      focusVisitorIp(ip);
    });
  });
}

let logPollTimer;
let mapPollTimer;
let statsPollTimer;

function shouldPollStatsFromStorage() {
  try {
    const raw = window.localStorage.getItem("admin.logs.windowState.v1");
    if (!raw) return true;
    const parsed = JSON.parse(raw);
    const mode = parsed?.stats?.mode;
    return mode !== "closed";
  } catch {
    return true;
  }
}

function startStatsPolling() {
  if (statsPollTimer) return;
  refreshStats();
  statsPollTimer = window.setInterval(refreshStats, 5000);
}

function stopStatsPolling() {
  if (!statsPollTimer) return;
  window.clearInterval(statsPollTimer);
  statsPollTimer = null;
}

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function setupLogConsoleWindowControls() {
  const splitEl = document.getElementById("adminLogSplit");
  const serverPane = document.getElementById("serverConsolePane");
  const pollerPane = document.getElementById("pollerConsolePane");
  const statsPane = document.getElementById("statsConsolePane");
  const handleA = document.getElementById("adminLogSplitHandleA");
  const handleB = document.getElementById("adminLogSplitHandleB");
  const emptyEl = document.getElementById("adminLogEmpty");
  const taskbarEl = document.getElementById("adminLogTaskbar");
  if (!splitEl || !serverPane || !pollerPane || !statsPane || !handleA || !handleB || !emptyEl || !taskbarEl) return;

  const ensurePollerPaneControlsMount = () => {
    const existing = pollerPane.querySelector?.("#adminPollerPaneControls");
    if (existing) return existing;
    const titlebar = pollerPane.querySelector?.(".admin-console-titlebar");
    if (!titlebar) return null;
    const mount = document.createElement("span");
    mount.id = "adminPollerPaneControls";
    mount.className = "admin-poller-pane-controls";
    // Put at the far right: title has margin-left:auto.
    titlebar.appendChild(mount);
    return mount;
  };

  const prefersReducedMotion = () => {
    const reduced = !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    const isMobile = !!window.matchMedia?.("(max-width: 768px)")?.matches;
    // On mobile, avoid animated show/hide because it causes visible layout jumping.
    return reduced || isMobile;
  };
  const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r()));

  const forceScrollLogsToBottom = (consoleKey) => {
    const viewer = consoleKey === "server" ? serverLogViewer : (consoleKey === "poller" ? pollerLogViewer : null);
    const preEl = viewer?.preEl || (consoleKey === "server"
      ? document.getElementById("serverConsole")
      : (consoleKey === "poller" ? document.getElementById("pollerConsole") : null));
    if (!preEl) return;
    // Force after layout so clientHeight is correct.
    requestAnimationFrame(() => {
      preEl.scrollTop = preEl.scrollHeight;
    });
  };

  const animatePaneVisibility = async (el, shouldShow, opts) => {
    if (!el) return;
    if (prefersReducedMotion()) {
      el.style.display = shouldShow ? "" : "none";
      el.classList.toggle("admin-console-wrap--anim-hide", !shouldShow);
      el.classList.remove("admin-console-wrap--anim-to-tray");
      el.classList.toggle("admin-console-wrap--anim-show", shouldShow);
      return;
    }

    // If visibility isn't changing, don't replay the animation.
    const isDisplayed = el.style.display !== "none";
    if (shouldShow && isDisplayed && el.classList.contains("admin-console-wrap--anim-show")) {
      return;
    }
    if (!shouldShow && !isDisplayed) {
      return;
    }

    if (shouldShow) {
      el.style.display = "";
      // Start from hidden state then transition to shown.
      el.classList.remove("admin-console-wrap--anim-show");
      el.classList.remove("admin-console-wrap--anim-to-tray");
      el.classList.remove("admin-console-wrap--anim-hide");

      const fromEl = opts?.fromEl || null;
      if (fromEl && !fromEl.hidden) {
        const to = el.getBoundingClientRect();
        const from = fromEl.getBoundingClientRect();
        const toCx = to.left + to.width / 2;
        const toCy = to.top + to.height / 2;
        const fromCx = from.left + from.width / 2;
        const fromCy = from.top + from.height / 2;
        const dx = fromCx - toCx;
        const dy = fromCy - toCy;
        el.style.setProperty("--tray-dx", `${Math.round(dx)}px`);
        el.style.setProperty("--tray-dy", `${Math.round(dy)}px`);
        el.classList.add("admin-console-wrap--anim-to-tray");
      } else {
        el.classList.add("admin-console-wrap--anim-hide");
      }
      // Force layout so the initial transform/opacity is committed before we animate to "show".
      // Without this, when other panes are already visible, the browser can coalesce style changes
      // and the open animation becomes imperceptible.
      void el.getBoundingClientRect();
      await nextFrame();
      await nextFrame();
      el.classList.remove("admin-console-wrap--anim-hide");
      el.classList.remove("admin-console-wrap--anim-to-tray");
      el.classList.add("admin-console-wrap--anim-show");
      el.style.removeProperty("--tray-dx");
      el.style.removeProperty("--tray-dy");
      return;
    }

    // Hide: animate then set display none.
    el.classList.remove("admin-console-wrap--anim-show");
    el.classList.remove("admin-console-wrap--anim-hide");
    el.classList.remove("admin-console-wrap--anim-to-tray");

    const toEl = opts?.toEl || null;
    if (toEl && !toEl.hidden) {
      const from = el.getBoundingClientRect();
      const to = toEl.getBoundingClientRect();
      const fromCx = from.left + from.width / 2;
      const fromCy = from.top + from.height / 2;
      const toCx = to.left + to.width / 2;
      const toCy = to.top + to.height / 2;
      const dx = toCx - fromCx;
      const dy = toCy - fromCy;
      el.style.setProperty("--tray-dx", `${Math.round(dx)}px`);
      el.style.setProperty("--tray-dy", `${Math.round(dy)}px`);
      el.classList.add("admin-console-wrap--anim-to-tray");
    } else {
      el.classList.add("admin-console-wrap--anim-hide");
    }
    // Commit initial state before transition begins.
    void el.getBoundingClientRect();

    const done = new Promise((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        el.removeEventListener("transitionend", onEnd);
        resolve();
      };
      const onEnd = (ev) => {
        if (ev.target !== el) return;
        finish();
      };
      el.addEventListener("transitionend", onEnd);
      window.setTimeout(finish, 260);
    });
    await done;
    el.style.display = "none";
    el.style.removeProperty("--tray-dx");
    el.style.removeProperty("--tray-dy");
  };

  const animateAuxVisibility = async (el, hideClass, shouldShow) => {
    if (!el) return;
    if (prefersReducedMotion()) {
      el.hidden = !shouldShow;
      el.classList.toggle(hideClass, !shouldShow);
      return;
    }

    if (shouldShow) {
      el.hidden = false;
      el.classList.add(hideClass);
      await nextFrame();
      await nextFrame();
      el.classList.remove(hideClass);
      return;
    }

    el.classList.add(hideClass);
    const done = new Promise((resolve) => window.setTimeout(resolve, 240));
    await done;
    el.hidden = true;
  };

  const storageKey = "admin.logs.windowState.v1";
  const pollerHintEl = document.getElementById("adminPollerHint");

  const setPollerHint = (text, isWarn = false) => {
    if (!pollerHintEl) return;
    pollerHintEl.textContent = text || "";
    pollerHintEl.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  const stopPollerProcess = async () => {
    setPollerHint("Stopping poller…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/stop-poller", { method: "POST" });
      setPollerHint("Poller stopped.");
      return payload;
    } catch (e) {
      setPollerHint(adminEndpointErrorMessage(e, "Stop poller"), true);
      throw e;
    }
  };

  const restartPollerProcess = async () => {
    setPollerHint("Restarting poller…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/restart-poller", { method: "POST" });
      const pid = payload?.start?.pid ?? payload?.pid;
      setPollerHint(pid ? `Poller restarted (pid ${pid}).` : "Poller restart triggered.");
      return payload;
    } catch (e) {
      setPollerHint(adminEndpointErrorMessage(e, "Start poller"), true);
      throw e;
    }
  };

  const resetLogViewerToSessionStart = (viewer) => {
    if (!viewer) return;
    viewer.entries = [];
    // Force refreshLogs() to do a full snapshot request (no cursor) next time.
    viewer.cursor = null;
    viewer._filterKey = "";
    if (viewer.preEl) viewer.preEl.innerHTML = "";
  };

  const defaultState = {
    server: { mode: "open" }, // open | minimized | closed
    poller: { mode: "open" }, // open | minimized | closed
    stats: { mode: "minimized" }, // open | minimized | closed
  };

  const readState = () => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (!raw) return structuredClone ? structuredClone(defaultState) : JSON.parse(JSON.stringify(defaultState));
      const parsed = JSON.parse(raw);
      const norm = (v) => (v === "open" || v === "minimized" || v === "closed" ? v : "open");
      return {
        server: { mode: norm(parsed?.server?.mode) },
        poller: { mode: norm(parsed?.poller?.mode) },
        stats: { mode: norm(parsed?.stats?.mode) },
      };
    } catch {
      return structuredClone ? structuredClone(defaultState) : JSON.parse(JSON.stringify(defaultState));
    }
  };

  let state = readState();

  const saveState = () => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(state));
    } catch {
      // ignore storage failures
    }
  };

  const isVisible = (consoleKey) => state[consoleKey].mode === "open";
  const isMinimized = (consoleKey) => state[consoleKey].mode === "minimized";
  const isClosed = (consoleKey) => state[consoleKey].mode === "closed";

  const labelFor = (consoleKey) =>
    consoleKey === "server" ? "server.log" : (consoleKey === "poller" ? "poller.log" : "stats");

  const dotColorFor = (consoleKey) =>
    consoleKey === "server" ? "#28c840" : (consoleKey === "poller" ? "#febc2e" : "#ff7a2f");

  const setMode = (consoleKey, mode) => {
    state[consoleKey].mode = mode;

    // Mobile UX: only one pane visible at a time.
    // Opening a pane replaces the currently-open one (others become minimized).
    const isMobile = () => !!window.matchMedia?.("(max-width: 768px)")?.matches;
    if (mode === "open" && isMobile()) {
      ["server", "poller", "stats"].forEach((k) => {
        if (k === consoleKey) return;
        if (state[k]?.mode === "open") state[k].mode = "minimized";
      });
    }

    saveState();
    if (consoleKey === "stats") {
      if (mode === "closed") stopStatsPolling();
      else startStatsPolling();
    }
    render();
  };

  const closeConsole = (consoleKey) => setMode(consoleKey, "closed");
  const minimizeConsole = (consoleKey) => setMode(consoleKey, "minimized");

  const restoreConsole = (consoleKey) => {
    if (isClosed("server") && isClosed("poller") && isClosed("stats")) {
      state.server.mode = consoleKey === "server" ? "open" : "closed";
      state.poller.mode = consoleKey === "poller" ? "open" : "closed";
      state.stats.mode = consoleKey === "stats" ? "open" : "closed";
      saveState();
      render();
      if (consoleKey === "server" || consoleKey === "poller") forceScrollLogsToBottom(consoleKey);
      return;
    }
    setMode(consoleKey, "open");
    if (consoleKey === "server" || consoleKey === "poller") forceScrollLogsToBottom(consoleKey);
  };

  const toggleFromDock = (consoleKey) => {
    const mode = state[consoleKey].mode;
    if (mode === "open") {
      minimizeConsole(consoleKey);
      return;
    }
    // If poller is "closed", treat dock click as "start it back up".
    if (consoleKey === "poller" && mode === "closed") {
      void (async () => {
        try {
          await restartPollerProcess();
          // After a stop→start, show only the new run's logs.
          resetLogViewerToSessionStart(pollerLogViewer);
          await refreshLogs();
          restoreConsole(consoleKey);
        } catch {
          // hint already set
        }
      })();
      return;
    }
    restoreConsole(consoleKey);
  };

  const maximizeConsole = (consoleKey) => {
    state[consoleKey].mode = "open";
    // Requirement: maximize current console, minimizing the other ones.
    ["server", "poller", "stats"].forEach((k) => {
      if (k === consoleKey) return;
      if (!isClosed(k)) state[k].mode = "minimized";
    });
    saveState();
    render();
    if (consoleKey === "server" || consoleKey === "poller") forceScrollLogsToBottom(consoleKey);
  };

  const renderTaskbar = async () => {
    taskbarEl.innerHTML = "";
    // Always-visible "dock" with fixed slots for each console.
    const allKeys = ["server", "poller", "stats"];
    allKeys.forEach((k) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const mode = state[k].mode;
      const modeClass =
        mode === "open"
          ? "admin-console-taskbar-btn--open"
          : (mode === "minimized" ? "admin-console-taskbar-btn--minimized" : "admin-console-taskbar-btn--closed");
      btn.className = `admin-console-taskbar-btn ${modeClass}`.trim();
      btn.setAttribute("data-console", k);
      btn.setAttribute("aria-label", `${mode === "open" ? "Minimize" : "Open"} ${labelFor(k)}`);

      const dot = document.createElement("span");
      dot.className = "admin-console-taskbar-dot";
      dot.style.background = mode === "closed" ? "rgba(148, 163, 184, 0.55)" : dotColorFor(k);
      btn.appendChild(dot);

      const text = document.createElement("span");
      text.textContent = labelFor(k);
      btn.appendChild(text);

      btn.addEventListener("click", () => toggleFromDock(k));
      taskbarEl.appendChild(btn);
    });

    // Poller controls live in the poller window titlebar (not in the taskbar).
    const pollerMount = ensurePollerPaneControlsMount();
    if (pollerMount) {
      pollerMount.innerHTML = "";
      const stopBtn = document.createElement("button");
      stopBtn.type = "button";
      stopBtn.className = "admin-console-dock-action admin-console-dock-action--stop";
      stopBtn.title = "Stop poller";
      stopBtn.setAttribute("aria-label", "Stop poller");
      stopBtn.textContent = "■";
      stopBtn.disabled = state.poller.mode === "closed";
      stopBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        void (async () => {
          try {
            await stopPollerProcess();
            // Keep the poller console open; just stop the process.
            restoreConsole("poller");
            await refreshLogs();
          } catch {
            // hint already set
          }
        })();
      });

      const restartBtn = document.createElement("button");
      restartBtn.type = "button";
      restartBtn.className = "admin-console-dock-action admin-console-dock-action--restart";
      restartBtn.title = "Restart poller";
      restartBtn.setAttribute("aria-label", "Restart poller");
      restartBtn.textContent = "↻";
      restartBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        void (async () => {
          try {
            await restartPollerProcess();
            resetLogViewerToSessionStart(pollerLogViewer);
            await refreshLogs();
            restoreConsole("poller");
          } catch {
            // hint already set
          }
        })();
      });

      pollerMount.appendChild(stopBtn);
      pollerMount.appendChild(restartBtn);
    }

    // Build per-console target map for animations.
    dockTargets = {
      server: taskbarEl.querySelector('button[data-console="server"]'),
      poller: taskbarEl.querySelector('button[data-console="poller"]'),
      stats: taskbarEl.querySelector('button[data-console="stats"]'),
    };
  };

  const renderLayout = async () => {
    const serverVisible = isVisible("server");
    const pollerVisible = isVisible("poller");
    const statsVisible = isVisible("stats");

    // Show/hide panes based on mode.
    await Promise.all([
      animatePaneVisibility(serverPane, serverVisible, { toEl: dockTargets?.server, fromEl: dockTargets?.server }),
      animatePaneVisibility(pollerPane, pollerVisible, { toEl: dockTargets?.poller, fromEl: dockTargets?.poller }),
      animatePaneVisibility(statsPane, statsVisible, { toEl: dockTargets?.stats, fromEl: dockTargets?.stats }),
    ]);

    // Show handles only when both adjacent panes are visible.
    // Special case: if poller is closed but server+stats are open, handleA becomes the divider.
    handleA.style.display = (serverVisible && pollerVisible) || (serverVisible && statsVisible && !pollerVisible) ? "" : "none";
    handleB.style.display = pollerVisible && statsVisible ? "" : "none";

    // Re-apply split ratios when visibility changes (e.g. stats closed).
    window.__adminLogSplit?.apply?.();

    // Show placeholder if none visible.
    const anyVisible = serverVisible || pollerVisible || statsVisible;
    await animateAuxVisibility(emptyEl, "admin-console-empty--anim-hide", !anyVisible);
    if (!anyVisible) {
      emptyEl.innerHTML = "";
      const msg = document.createElement("div");
      msg.textContent = "No console active";
      emptyEl.appendChild(msg);
    }
  };

  let renderToken = 0;
  let dockTargets = { server: null, poller: null, stats: null };
  const render = async () => {
    const token = (renderToken += 1);
    // Ensure dock is rendered first so animations have a per-console target.
    await renderTaskbar();
    if (token !== renderToken) return;
    await renderLayout();
  };

  // Wire dot buttons.
  splitEl.addEventListener("click", (ev) => {
    const btn = ev.target?.closest?.("button.admin-console-dot");
    if (!btn) return;
    if (btn.disabled) return;
    const consoleKey = btn.getAttribute("data-console");
    const action = btn.getAttribute("data-action");
    if (consoleKey !== "server" && consoleKey !== "poller" && consoleKey !== "stats") return;
    if (action === "close") {
      // Server close is disabled (never stop server from this UI).
      if (consoleKey === "server") return;
      // Poller close should actually stop the process.
      if (consoleKey === "poller") {
        void (async () => {
          try {
            await stopPollerProcess();
          } finally {
            // Ensure reopening doesn't show pre-stop logs.
            resetLogViewerToSessionStart(pollerLogViewer);
            closeConsole("poller");
            await refreshLogs();
          }
        })();
        return;
      }
      closeConsole(consoleKey);
      return;
    }
    else if (action === "minimize") minimizeConsole(consoleKey);
    else if (action === "maximize") maximizeConsole(consoleKey);
  });

  // Clicking inside a log pane should jump to the latest logs.
  splitEl.addEventListener("click", (ev) => {
    const pane = ev.target?.closest?.(".admin-console-wrap");
    if (!pane) return;
    const id = pane.getAttribute("id") || "";
    if (id === "serverConsolePane") forceScrollLogsToBottom("server");
    else if (id === "pollerConsolePane") forceScrollLogsToBottom("poller");
  });

  render();

  // Expose tiny hook for split view setup (so it can re-render after ratio changes).
  window.__adminLogWindowState = {
    getVisiblePair: () => ({ server: isVisible("server"), poller: isVisible("poller"), stats: isVisible("stats") }),
    render,
  };
}

function setupLogSplitView() {
  const splitEl = document.getElementById("adminLogSplit");
  const handleA = document.getElementById("adminLogSplitHandleA");
  const handleB = document.getElementById("adminLogSplitHandleB");
  const serverPane = document.getElementById("serverConsolePane");
  const pollerPane = document.getElementById("pollerConsolePane");
  const statsPane = document.getElementById("statsConsolePane");
  if (!splitEl || !handleA || !handleB || !serverPane || !pollerPane || !statsPane) return;

  const tripleKey = "admin.logSplit.triple.v1";
  const doubleKey = "admin.logSplit.double.v1";
  const serverStatsKey = "admin.logSplit.double.serverStats.v1";
  const pollerStatsKey = "admin.logSplit.double.pollerStats.v1";
  const minServer = 0.21; // server pane at least 21%
  const minStats = 0.2; // stats pane at least 20%
  const min = 0.18; // other panes at least 18%
  const step = 0.02;

  const _width = (el) => {
    if (!el) return 0;
    const rect = el.getBoundingClientRect();
    return Number.isFinite(rect.width) ? rect.width : 0;
  };

  const _outerWidth = (el) => {
    if (!el) return 0;
    // When display:none, width is 0.
    const w = _width(el);
    if (!w) return 0;
    try {
      const cs = window.getComputedStyle(el);
      const ml = Number.parseFloat(cs.marginLeft || "0") || 0;
      const mr = Number.parseFloat(cs.marginRight || "0") || 0;
      return w + ml + mr;
    } catch {
      return w;
    }
  };

  const ratiosForVisibility = (vis) => {
    const totalW = _width(splitEl);
    // Handles have horizontal margins; include them so pane min% matches the visible %.
    const wA =
      vis?.server && (vis?.poller || (vis?.stats && !vis?.poller)) ? _outerWidth(handleA) : 0;
    const wB = vis?.poller && vis?.stats ? _outerWidth(handleB) : 0;
    const paneW = Math.max(1, totalW - wA - wB);

    // Convert "min % of total container" into min ratio of the pane area.
    const minServerRatio = clamp((minServer * totalW) / paneW, 0, 1);
    const minOtherRatio = clamp((min * totalW) / paneW, 0, 1);
    const minStatsRatio = clamp((minStats * totalW) / paneW, 0, 1);
    return { minServerRatio, minOtherRatio, minStatsRatio };
  };

  const clampDouble = (ratio, vis) => {
    const { minServerRatio, minOtherRatio } = ratiosForVisibility(vis);
    return clamp(Number(ratio) || 0.5, minServerRatio, 1 - minOtherRatio);
  };

  const clampSplit = (x1, x2, vis) => {
    let a = Number(x1);
    let b = Number(x2);
    if (!Number.isFinite(a)) a = 0.34;
    if (!Number.isFinite(b)) b = 0.68;
    const { minServerRatio, minOtherRatio, minStatsRatio } = ratiosForVisibility(vis);
    // a is the server split point; enforce server min explicitly.
    a = clamp(a, minServerRatio, 1 - 2 * minOtherRatio);
    // b splits poller vs stats; enforce poller>=minOther and stats>=minStats.
    b = clamp(b, a + minOtherRatio, 1 - minStatsRatio);
    return [a, b];
  };

  const clampServerStats = (ratio, vis) => {
    const { minServerRatio, minStatsRatio } = ratiosForVisibility(vis);
    return clamp(Number(ratio) || 0.5, minServerRatio, 1 - minStatsRatio);
  };

  const clampPollerStats = (ratio, vis) => {
    const { minOtherRatio, minStatsRatio } = ratiosForVisibility(vis);
    // ratio is poller share
    return clamp(Number(ratio) || 0.5, minOtherRatio, 1 - minStatsRatio);
  };

  const applyDouble = (ratio) => {
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (vis && (!vis.server || !vis.poller)) return;
    const r = clampDouble(ratio, vis);
    serverPane.style.flex = `${r} 1 0`;
    pollerPane.style.flex = `${1 - r} 1 0`;
    statsPane.style.flex = "";
    handleA.setAttribute("aria-valuenow", String(Math.round(r * 100)));
    handleA.setAttribute("aria-valuemin", String(Math.round(minServer * 1000) / 10));
    handleA.setAttribute("aria-valuemax", String(Math.round((1 - min) * 1000) / 10));
  };

  const applyTriple = (x1, x2) => {
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (vis && (!vis.server || !vis.poller || !vis.stats)) return;
    const [a, b] = clampSplit(x1, x2, vis);
    serverPane.style.flex = `${a} 1 0`;
    pollerPane.style.flex = `${b - a} 1 0`;
    statsPane.style.flex = `${1 - b} 1 0`;
    handleA.setAttribute("aria-valuenow", String(Math.round(a * 100)));
    handleA.setAttribute("aria-valuemin", String(Math.round(minServer * 1000) / 10));
    handleA.setAttribute("aria-valuemax", String(Math.round((1 - 2 * min) * 100)));
    handleB.setAttribute("aria-valuenow", String(Math.round(b * 100)));
    handleB.setAttribute("aria-valuemin", String(Math.round((min * 2) * 100)));
    handleB.setAttribute("aria-valuemax", String(Math.round((1 - min) * 100)));
  };

  const applyServerStats = (ratio) => {
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (vis && (!vis.server || !vis.stats || vis.poller)) return;
    const r = clampServerStats(ratio, vis);
    serverPane.style.flex = `${r} 1 0`;
    pollerPane.style.flex = "";
    statsPane.style.flex = `${1 - r} 1 0`;
    handleA.setAttribute("aria-valuenow", String(Math.round(r * 100)));
    handleA.setAttribute("aria-valuemin", String(Math.round(minServer * 1000) / 10));
    handleA.setAttribute("aria-valuemax", String(Math.round((1 - minStats) * 1000) / 10));
  };

  const readTriple = () => {
    try {
      const raw = window.localStorage.getItem(tripleKey);
      if (!raw) return [0.34, 0.68];
      const parsed = JSON.parse(raw);
      return clampSplit(parsed?.x1, parsed?.x2, window.__adminLogWindowState?.getVisiblePair?.());
    } catch {
      return [0.34, 0.68];
    }
  };

  const saveTriple = (x1, x2) => {
    try {
      const [a, b] = clampSplit(x1, x2, window.__adminLogWindowState?.getVisiblePair?.());
      window.localStorage.setItem(tripleKey, JSON.stringify({ x1: a, x2: b }));
    } catch {
      // ignore storage failures
    }
  };

  const readDouble = () => {
    try {
      const raw = window.localStorage.getItem(doubleKey);
      if (!raw) return 0.5;
      const parsed = JSON.parse(raw);
      return clampDouble(parsed?.ratio, window.__adminLogWindowState?.getVisiblePair?.());
    } catch {
      return 0.5;
    }
  };

  const saveDouble = (ratio) => {
    try {
      window.localStorage.setItem(
        doubleKey,
        JSON.stringify({ ratio: clampDouble(ratio, window.__adminLogWindowState?.getVisiblePair?.()) }),
      );
    } catch {
      // ignore storage failures
    }
  };

  const readServerStats = () => {
    try {
      const raw = window.localStorage.getItem(serverStatsKey);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const r = clampServerStats(parsed?.ratio, window.__adminLogWindowState?.getVisiblePair?.());
      return Number.isFinite(r) ? r : null;
    } catch {
      return null;
    }
  };

  const saveServerStats = (ratio) => {
    try {
      window.localStorage.setItem(
        serverStatsKey,
        JSON.stringify({ ratio: clampServerStats(ratio, window.__adminLogWindowState?.getVisiblePair?.()) }),
      );
    } catch {
      // ignore storage failures
    }
  };

  const readPollerStats = () => {
    try {
      const raw = window.localStorage.getItem(pollerStatsKey);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const r = clampPollerStats(parsed?.ratio, window.__adminLogWindowState?.getVisiblePair?.());
      return Number.isFinite(r) ? r : null;
    } catch {
      return null;
    }
  };

  const savePollerStats = (ratio) => {
    try {
      window.localStorage.setItem(
        pollerStatsKey,
        JSON.stringify({ ratio: clampPollerStats(ratio, window.__adminLogWindowState?.getVisiblePair?.()) }),
      );
    } catch {
      // ignore storage failures
    }
  };

  let [x1, x2] = readTriple();
  let ratio2 = readDouble();
  let ratioSS = readServerStats();
  let ratioPS = readPollerStats();

  const applyFromVisibility = () => {
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    // If only one pane is visible, let it take full width.
    if (vis && vis.server && !vis.poller && !vis.stats) {
      serverPane.style.flex = "1 1 0";
      pollerPane.style.flex = "";
      statsPane.style.flex = "";
      return;
    }
    if (vis && !vis.server && vis.poller && !vis.stats) {
      serverPane.style.flex = "";
      pollerPane.style.flex = "1 1 0";
      statsPane.style.flex = "";
      return;
    }
    if (vis && !vis.server && !vis.poller && vis.stats) {
      serverPane.style.flex = "";
      pollerPane.style.flex = "";
      statsPane.style.flex = "1 1 0";
      return;
    }
    // If poller is closed but server + stats are open, allow a dedicated 2-pane split.
    if (vis && vis.server && !vis.poller && vis.stats) {
      const fallbackFromTriple = (() => {
        const [a] = clampSplit(x1, x2, vis);
        return clampServerStats(a, vis);
      })();
      const r = ratioSS == null ? fallbackFromTriple : clampServerStats(ratioSS, vis);
      applyServerStats(r);
      return;
    }
    // If server is minimized/closed but poller + stats are open, allow a dedicated 2-pane split.
    if (vis && !vis.server && vis.poller && vis.stats) {
      const fallbackFromTriple = (() => {
        const [, b] = clampSplit(x1, x2, vis);
        return clampPollerStats(b, vis);
      })();
      const r = ratioPS == null ? fallbackFromTriple : clampPollerStats(ratioPS, vis);
      serverPane.style.flex = "";
      pollerPane.style.flex = `${r} 1 0`;
      statsPane.style.flex = `${1 - r} 1 0`;
      return;
    }
    if (vis && vis.server && vis.poller && vis.stats) {
      applyTriple(x1, x2);
      return;
    }
    if (vis && vis.server && vis.poller) {
      applyDouble(ratio2);
    }
  };

  applyFromVisibility();

  const ratioFromClientX = (clientX) => {
    const rect = splitEl.getBoundingClientRect();
    const x = clamp(clientX - rect.left, 0, rect.width);
    return rect.width > 0 ? x / rect.width : 0.5;
  };

  const startDrag = (handleEl, which, ev) => {
    if (window.matchMedia?.("(max-width: 768px)")?.matches) return;
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (which === "a") {
      if (vis && (!vis.server || (!vis.poller && !vis.stats))) return;
    } else {
      if (vis && (!vis.poller || !vis.stats)) return;
    }
    handleEl.dataset.dragging = "true";
    const pointerId = ev.pointerId;
    handleEl.setPointerCapture?.(pointerId);
    ev.preventDefault();

    const onMove = (moveEv) => {
      const r = ratioFromClientX(moveEv.clientX);
      if (which === "a") {
        // If poller is not visible, handleA becomes a server↔stats splitter.
        if (vis && vis.server && !vis.poller && vis.stats) {
          ratioSS = clampServerStats(r, vis);
          applyServerStats(ratioSS);
          saveServerStats(ratioSS);
          return;
        }
        // If stats is not visible, handleA becomes a simple 2-pane splitter.
        if (vis && vis.server && vis.poller && !vis.stats) {
          ratio2 = clampDouble(r, vis);
          applyDouble(ratio2);
          saveDouble(ratio2);
          return;
        }
        [x1, x2] = clampSplit(r, x2, vis);
        applyTriple(x1, x2);
        saveTriple(x1, x2);
        return;
      }

      // handleB: if server is not visible, make it a 2-pane poller↔stats splitter.
      if (vis && !vis.server && vis.poller && vis.stats) {
        ratioPS = clampPollerStats(r, vis);
        applyFromVisibility();
        savePollerStats(ratioPS);
        return;
      }

      [x1, x2] = clampSplit(x1, r, vis);
      applyTriple(x1, x2);
      saveTriple(x1, x2);
    };

    const stop = () => {
      handleEl.dataset.dragging = "false";
      handleEl.releasePointerCapture?.(pointerId);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
  };

  handleA.addEventListener("pointerdown", (ev) => startDrag(handleA, "a", ev));
  handleB.addEventListener("pointerdown", (ev) => startDrag(handleB, "b", ev));

  const onKey = (which, ev) => {
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (which === "a") {
      if (vis && (!vis.server || (!vis.poller && !vis.stats))) return;
    } else {
      if (vis && (!vis.poller || !vis.stats)) return;
    }
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    const delta = ev.key === "ArrowLeft" ? -step : step;
    if (which === "a") {
      if (vis && vis.server && !vis.poller && vis.stats) {
        ratioSS = clampServerStats((ratioSS == null ? 0.5 : ratioSS) + delta, vis);
        applyServerStats(ratioSS);
        saveServerStats(ratioSS);
      } else
        if (vis && vis.server && vis.poller && !vis.stats) {
          ratio2 = clampDouble(ratio2 + delta, vis);
          applyDouble(ratio2);
          saveDouble(ratio2);
        } else {
          [x1, x2] = clampSplit(x1 + delta, x2, vis);
          applyTriple(x1, x2);
          saveTriple(x1, x2);
        }
    } else {
      if (vis && !vis.server && vis.poller && vis.stats) {
        ratioPS = clampPollerStats((ratioPS == null ? 0.5 : ratioPS) + delta, vis);
        applyFromVisibility();
        savePollerStats(ratioPS);
      } else {
        [x1, x2] = clampSplit(x1, x2 + delta, vis);
        applyTriple(x1, x2);
        saveTriple(x1, x2);
      }
    }
    ev.preventDefault();
  };

  handleA.addEventListener("keydown", (ev) => onKey("a", ev));
  handleB.addEventListener("keydown", (ev) => onKey("b", ev));

  // Let the window manager re-apply ratios when panes open/close.
  window.__adminLogSplit = {
    apply: applyFromVisibility,
  };
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

    const parseIsoAssumeUtc = (raw) => {
      const s = String(raw || "").trim();
      if (!s) return null;
      // If the ISO string has no timezone suffix, assume it is UTC.
      // (Browsers treat "2026-04-29T12:00:00" as local time, which makes UTC logs look "stuck" in UTC.)
      const hasTz = /([zZ]|[+\-]\d{2}:\d{2})$/.test(s);
      const normalized = hasTz ? s : `${s}Z`;
      const d = new Date(normalized);
      return Number.isNaN(d.getTime()) ? null : d;
    };

    let html = "";
    for (const entry of this.entries) {
      const levelRaw = String(entry?.level || "info").toLowerCase();
      const level = levelRaw === "warning" ? "warn" : (levelRaw === "error" ? "error" : "info");
      const tsRaw = entry?.ts ? String(entry.ts) : "";
      let ts = "";
      if (tsRaw) {
        const d = parseIsoAssumeUtc(tsRaw);
        ts = d
          ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
          : tsRaw;
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
let logRefreshSeq = 0;

function isEditingAdminDatalist() {
  const el = document.activeElement;
  if (!el) return false;
  // When typing in a <input list="...">, browsers can dismiss the suggestions on unrelated DOM churn.
  // We pause log/visitor rendering while the user is interacting with these controls.
  if (el.id === "adminSalesItemInput") return true;
  if (typeof el.closest === "function" && el.closest(".admin-data-tools")) return true;
  return false;
}

function appendLocalConsoleLine(viewer, { msg, level = "info" }) {
  if (!viewer) return;
  const entry = {
    ts: new Date().toISOString(),
    level,
    msg,
    name: "admin",
  };
  viewer.appendEntries({ entries: [entry] });
}

async function refreshLogs() {
  // Keep <datalist> suggestions stable while the user is selecting an item.
  if (isEditingAdminDatalist()) {
    return;
  }
  const seq = (logRefreshSeq += 1);
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
          ...(filterChanged || viewer?.cursor == null ? {} : { cursor: String(viewer?.cursor || 0) }),
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
    // If a newer refresh started while we were awaiting, ignore this one.
    if (seq !== logRefreshSeq) return;

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
      hint.textContent = adminEndpointErrorMessage(e, "Logs");
    }
  }

  window.__adminEqualizePanes?.schedule?.();
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

// When panes get narrow, toolbars can wrap and change the pane height.
// Keep all visible panes the same height by matching the tallest.
function setupEqualHeightConsolePanes() {
  // On mobile we force fixed pane heights in CSS; this desktop-only logic can cause flicker.
  if (window.matchMedia?.("(max-width: 768px)")?.matches) return;

  const splitEl = document.getElementById("adminLogSplit");
  if (!splitEl) return;

  const isVisible = (el) => {
    if (!el) return false;
    try {
      return window.getComputedStyle(el).display !== "none";
    } catch {
      return true;
    }
  };

  let syncing = false;
  let queued = false;

  const sync = () => {
    if (syncing) return;
    syncing = true;
    queued = false;

    const panes = Array.from(splitEl.querySelectorAll(".admin-console-wrap"));

    // Reset any previous equalization so measurements are natural.
    panes.forEach((pane) => {
      pane.style.height = "";
      const consoleEl = pane.querySelector(".admin-console");
      if (consoleEl) {
        consoleEl.style.height = "";
        consoleEl.style.minHeight = "";
        consoleEl.style.maxHeight = "";
      }
      const panelBody = pane.querySelector(".admin-panel-body");
      if (panelBody) {
        panelBody.style.height = "";
      }
    });

    // Measure tallest visible pane.
    let maxH = 0;
    const visiblePanes = panes.filter(isVisible);
    visiblePanes.forEach((pane) => {
      const h = pane.getBoundingClientRect()?.height ?? 0;
      if (Number.isFinite(h) && h > maxH) maxH = h;
    });
    if (!maxH || !Number.isFinite(maxH)) {
      syncing = false;
      return;
    }

    // Apply: set pane height, then expand the body (console/panel body) to fill extra space.
    visiblePanes.forEach((pane) => {
      pane.style.height = `${Math.ceil(maxH)}px`;

      const consoleEl = pane.querySelector(".admin-console");
      if (consoleEl) {
        // pane height includes titlebar + toolbar + console body.
        const chromeH = pane.getBoundingClientRect().height - consoleEl.getBoundingClientRect().height;
        const bodyH = Math.max(120, Math.floor(maxH - chromeH));
        consoleEl.style.height = `${bodyH}px`;
        consoleEl.style.minHeight = `${bodyH}px`;
        consoleEl.style.maxHeight = `${bodyH}px`;
      }

      const panelBody = pane.querySelector(".admin-panel-body");
      if (panelBody) {
        const chromeH = pane.getBoundingClientRect().height - panelBody.getBoundingClientRect().height;
        const bodyH = Math.max(120, Math.floor(maxH - chromeH));
        panelBody.style.height = `${bodyH}px`;
      }
    });

    syncing = false;
  };

  const schedule = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      // Let layout settle, then measure.
      requestAnimationFrame(sync);
    });
  };

  schedule();
  window.addEventListener("resize", schedule);

  // Observe size changes caused by toolbar wrapping, fonts, etc.
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => {
      if (syncing) return;
      schedule();
    });
    ro.observe(splitEl);
    Array.from(splitEl.querySelectorAll(".admin-console-wrap")).forEach((pane) => ro.observe(pane));
  }

  // Expose a tiny hook so open/close/minimize/maximize can re-sync after animations.
  window.__adminEqualizePanes = { schedule };
}

function renderStatsCards(payload) {
  const grid = document.getElementById("adminStatsGrid");
  if (!grid) return;

  const cpu = payload?.system?.cpu || {};
  const mem = payload?.system?.memory || {};
  const swap = payload?.system?.swap || {};
  const net = payload?.system?.net || {};

  const cards = [
    { key: "cpu", k: "CPU", v: `${formatPercent(cpu.percent)}` },
    { key: "uptime", k: "Uptime", v: formatUptimeFromBootMs(payload?.system?.bootTimeMs) },
    { key: "ram", k: "RAM", v: `${formatPercent(mem.usedPercent)} · ${formatBytesMb(mem.usedMb)} / ${formatBytesMb(mem.totalMb)}` },
    { key: "sinceDeploy", k: "Since deploy", v: formatSinceDeployFromDeployTimeMs(payload?.app?.deployTimeMs) },
    { key: "swap", k: "Swap", v: `${formatPercent(swap.usedPercent)} · ${formatBytesMb(swap.usedMb)} / ${formatBytesMb(swap.totalMb)}` },
    { key: "net", k: "Network I/O", v: `${formatBytesMb(net.rxMb)} ↓ / ${formatBytesMb(net.txMb)} ↑` },
  ];

  grid.innerHTML = cards
    .map(
      (c) => `
    <div class="admin-stats-card admin-stats-card--${escapeHtml(c.key)}">
      <p class="admin-stats-k">${escapeHtml(c.k)}</p>
      <p class="admin-stats-v"><strong>${escapeHtml(c.v)}</strong></p>
    </div>`,
    )
    .join("");

  window.__adminEqualizePanes?.schedule?.();
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
    const ok = window.confirm(
      "Run the DB export/backup now?\n\nThis will snapshot the SQLite DB and upload it to the configured Discord webhook.\n\nContinue?",
    );
    if (!ok) return;

    btn.disabled = true;
    setHint("Running DB export…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/run-db-export", { method: "POST" });
      if (!payload?.ok) {
        setHint(payload?.error ? `DB export: ${payload.error}` : "DB export failed.", true);
        return;
      }
      const name = payload?.file || "export";
      const sizeMiB = payload?.sizeMiB;
      setHint(sizeMiB != null ? `DB export uploaded: ${name} (${sizeMiB} MiB).` : `DB export uploaded: ${name}.`);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "DB export"), true);
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
    const ok = window.confirm(
      "This will clear market data from SQLite (data/market.db).\n\nContinue?",
    );
    if (!ok) return;

    btn.disabled = true;
    setHint("Clearing data…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/clear-data", { method: "POST" });
      const cleared = payload?.cleared || {};
      const sqlite = cleared.sqlite ? "sqlite" : null;
      setHint(sqlite ? "Cleared: sqlite" : "Cleared.");
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Clear data"), true);
    } finally {
      btn.disabled = false;
    }
  });
}

function setupDeleteSalesTool() {
  const input = document.getElementById("adminSalesItemInput");
  const list = document.getElementById("adminSalesItemList");
  const preview = document.getElementById("adminSalesPreview");
  const saleSelect = document.getElementById("adminSalesEventSelect");
  const salePreview = document.getElementById("adminSalesEventPreview");
  const btn = document.getElementById("adminWipeVariantBtn");
  const resendBtn = document.getElementById("adminResendSaleAlertBtn");
  const hint = document.getElementById("adminSalesDeleteHint");
  if (!input || !list || !preview || !saleSelect || !salePreview || !btn || !resendBtn || !hint) return;

  const setHint = (text, isWarn = false) => {
    hint.textContent = text || "";
    hint.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  let variants = [];
  let byLabel = new Map();
  let selected = null;
  let sales = [];
  let saleGroups = [];

  const fmt = (v) => {
    const mode = v.mode ? ` · ${v.mode}` : "";
    const base = v.baseItemName ? ` · base: ${v.baseItemName}` : "";
    return `${v.displayName}${mode}${base}`;
  };

  const updatePreview = () => {
    const v = selected;
    if (!v) {
      preview.textContent = "Select an item variant.";
      btn.disabled = true;
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

  const filterListOptions = (query) => {
    const q = String(query || "").trim().toLowerCase();
    list.innerHTML = "";
    const matches = q
      ? variants.filter((v) => `${v.displayName} (${v.mode || "variant"})`.toLowerCase().includes(q))
      : variants;
    matches.forEach((v) => {
      const label = `${v.displayName} (${v.mode || "variant"})`;
      const opt = document.createElement("option");
      opt.value = label;
      list.appendChild(opt);
    });
  };

  const selectFromInput = () => {
    const raw = String(input.value || "").trim();
    selected = byLabel.get(raw) || null;
    updatePreview();
    void loadSalesForSelected();
  };

  input.addEventListener("change", selectFromInput);
  input.addEventListener("input", () => {
    filterListOptions(input.value);
    selectFromInput();
  });

  const updateDeleteBtnLabel = () => {
    const g = selectedGroup();
    btn.textContent = g ? "Delete selected sale" : "Delete all sales + fingerprints";
  };

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
        return;
      }
      setHint(`Resent sale alert for ${payload?.item || selected.displayName} (${g.label}, ${g.signals} signal(s)).`);
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Resend sale alert"), true);
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
        `Delete sale group for:\n\n${selected.displayName}\n` +
        `Timestamp: ${g.label} · ${g.signals} signal(s)\n\n` +
        `This will remove ${g.saleIds.length} sale record(s).\n` +
        `Fingerprints and inference state are NOT affected.\n\n` +
        `This cannot be undone.\n\nContinue?`;
      requestBody = { scope: "sales", variantId: selected.variantId, saleIds: g.saleIds };
    } else {
      msg =
        `Delete ALL sales + fingerprint state for:\n\n${selected.displayName}\n\n` +
        `This will remove:\n` +
        `- all sales rows\n` +
        `- listing snapshot fingerprints (used for inference)\n` +
        `- inference events/state fingerprints\n` +
        `- inferred sale counters ("Est. sold") for this variant\n\n` +
        `This cannot be undone.\n\nContinue?`;
      requestBody = { scope: "variant", variantId: selected.variantId };
    }
    const ok = window.confirm(msg);
    if (!ok) return;
    btn.disabled = true;
    setHint("Deleting\u2026");
    try {
      const payload = await fetchJsonWithInit("/api/admin/market/wipe-variant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      if (!payload?.ok) {
        setHint(payload?.error ? `Delete item data: ${payload.error}` : "Delete item data failed", true);
        return;
      }
      if (g) {
        const salesN = payload?.deleted?.sales ?? 0;
        setHint(`Deleted ${salesN} sale record(s) for ${selected.displayName} (${g.label}).`);
      } else {
        const salesN = payload?.deleted?.sales ?? payload?.deletedSales ?? 0;
        const listingsN = payload?.deleted?.listingSnapshots ?? 0;
        const eventsN = payload?.deleted?.inferenceEvents ?? 0;
        const pendingN = payload?.deleted?.inferencePending ?? 0;
        const signalsN = payload?.deleted?.inferenceSignals ?? 0;
        const pollsN = payload?.updated?.pollsReset ?? payload?.pollsUpdated ?? 0;
        setHint(
          `Deleted sales ${salesN}, listings ${listingsN}, events ${eventsN}, pending ${pendingN}, signals ${signalsN}. Reset polls ${pollsN}.`,
        );
      }
      await loadVariants();
      selectFromInput();
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Delete item data"), true);
    } finally {
      btn.disabled = false;
    }
  });

  async function loadVariants() {
    preview.textContent = "Loading…";
    btn.disabled = true;
    resendBtn.disabled = true;
    setHint("");
    try {
      const payload = await fetchJson("/api/admin/market/variants-sales");
      if (!payload?.ok) throw new Error(payload?.error || "Failed to load variants");
      variants = payload.variants || [];
      byLabel = new Map();
      variants.forEach((v) => {
        const label = `${v.displayName} (${v.mode || "variant"})`;
        byLabel.set(label, v);
      });
      filterListOptions(String(input.value || ""));
      preview.textContent = "Select an item variant.";
      sales = [];
      saleGroups = [];
      renderSalesSelect();
    } catch (e) {
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
  const MARKET_META_KEY = "market";
  const jsonEl = document.getElementById("marketCfgJson");
  const prettyEl = document.getElementById("marketCfgJsonPretty");
  const fieldsEl = document.getElementById("marketCfgFields");
  const keyEl = document.getElementById("marketCfgKey");
  const saveBtn = document.getElementById("marketCfgSaveBtn");
  const fmtBtn = document.getElementById("marketCfgFormatBtn");
  const reloadBtn = document.getElementById("marketCfgReloadBtn");
  const hintEl = document.getElementById("marketCfgHint");
  if (!jsonEl || !prettyEl || !fieldsEl || !saveBtn || !fmtBtn || !reloadBtn || !hintEl) return;

  const setHint = (text, isWarn = false) => {
    hintEl.textContent = text || "";
    hintEl.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
  };

  const formatConfigTimestamp = (value) => {
    // Expected input: ISO-8601 UTC string. Render: dd-MM-yyyy HH:mm
    if (!value) return "";
    const raw = value instanceof Date ? value.toISOString() : String(value);
    const hasTz = /([zZ]|[+\-]\d{2}:\d{2})$/.test(raw.trim());
    const d = value instanceof Date ? value : new Date(hasTz ? raw : `${raw}Z`);
    if (Number.isNaN(d.getTime())) return String(value);
    const pad2 = (n) => String(n).padStart(2, "0");
    const dd = pad2(d.getDate());
    const MM = pad2(d.getMonth() + 1);
    const yyyy = String(d.getFullYear());
    const HH = pad2(d.getHours());
    const mm = pad2(d.getMinutes());
    return `${dd}-${MM}-${yyyy} ${HH}:${mm}`;
  };

  let currentParsed = null; // object|array|primitive|null

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

  const META = {
    alert_enabled: {
      label: "Enable alerts",
      help: "Turns alerting on/off globally.",
      type: "boolean",
    },
    alert_threshold_pct: {
      label: "Threshold (%)",
      help: "Minimum percent drop required to trigger an alert.",
      type: "number",
    },
    alert_history_cycles: {
      label: "History cycles",
      help: "How many past cycles to compare against.",
      type: "number",
    },
    alert_min_total_results: {
      label: "Min results",
      help: "Ignore items with fewer total results than this.",
      type: "number",
    },
    alert_min_floor_listings: {
      label: "Min floor listings",
      help: "Minimum listings required to treat the floor as reliable.",
      type: "number",
    },
    alert_floor_band_pct: {
      label: "Floor band (%)",
      help: "Treat listings within this percent of the floor as the floor band.",
      type: "number",
    },
    alert_low_liquidity_extra_drop_pct: {
      label: "Low-liquidity extra drop (%)",
      help: "Extra drop required when liquidity is low.",
      type: "number",
    },
    alert_cooldown_cycles: {
      label: "Cooldown cycles",
      help: "Minimum cycles between alerts for the same item.",
      type: "number",
    },
    sales_discord_window_days: {
      label: "Sales Discord window (days)",
      help: 'Rolling window for "total est. sold" in Discord sale notifications (match your chart period, e.g. 90 ~ 3m preset).',
    },
    inference_listings_fetch_cap: {
      label: "Inference listings fetch cap",
      help: "Max search result IDs to fetch for sale inference per item (PoE caps at 100; 0 disables).",
    },
    inference_truncation_safe_margin_pct: {
      label: "Inference truncation safe margin (%)",
      help: "Safe margin for truncation of inference results. Anything above this margin is ignored.",
    },
  };

  const getSelectedKey = () => {
    const raw = String(keyEl?.value || "").trim();
    return raw || KEY_DEFAULT;
  };

  const renderFields = (value) => {
    fieldsEl.innerHTML = "";
    currentParsed = value;

    const selectedKey = getSelectedKey();
    const canUseMeta = selectedKey === MARKET_META_KEY;

    // Only render a nice editor for plain objects (the expected app_config shape).
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      const note = document.createElement("p");
      note.className = "admin-muted";
      note.textContent = "This value is not a JSON object. Use Advanced JSON.";
      fieldsEl.appendChild(note);
      return;
    }

    const keys = Object.keys(value).sort((a, b) => {
      if (canUseMeta) {
        const aHas = Object.prototype.hasOwnProperty.call(META, a);
        const bHas = Object.prototype.hasOwnProperty.call(META, b);
        if (aHas && !bHas) return -1;
        if (!aHas && bHas) return 1;
      }
      return a.localeCompare(b);
    });
    if (!keys.length) {
      const note = document.createElement("p");
      note.className = "admin-muted";
      note.textContent = "Empty object. Edit in Advanced JSON.";
      fieldsEl.appendChild(note);
      return;
    }

    for (const k of keys) {
      const row = document.createElement("div");
      row.className = "admin-marketcfg-field";

      const labelWrap = document.createElement("div");
      labelWrap.className = "admin-marketcfg-label";
      const meta = canUseMeta ? META[k] || null : null;
      const title = document.createElement("strong");
      title.textContent = meta?.label || k;
      const help = document.createElement("div");
      help.className = "admin-marketcfg-help";
      help.textContent = meta?.help || `Key: ${k}`;
      labelWrap.appendChild(title);
      labelWrap.appendChild(help);

      const wrap = document.createElement("div");
      wrap.className = "admin-marketcfg-control";

      const v = value[k];
      const inputId = `appcfg_${k}`;

      if (typeof v === "boolean") {
        const label = document.createElement("label");
        label.className = "admin-marketcfg-toggle";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.id = inputId;
        cb.checked = v;
        cb.setAttribute("data-key", k);
        cb.setAttribute("data-kind", "bool");
        label.appendChild(cb);
        const txt = document.createElement("span");
        txt.textContent = v ? "Enabled" : "Disabled";
        cb.addEventListener("change", () => {
          txt.textContent = cb.checked ? "Enabled" : "Disabled";
        });
        label.appendChild(txt);
        wrap.appendChild(label);
      } else if (typeof v === "number") {
        const inp = document.createElement("input");
        inp.type = "number";
        inp.className = "admin-appconfig-input admin-appconfig-num";
        inp.id = inputId;
        inp.value = String(v);
        inp.setAttribute("data-key", k);
        inp.setAttribute("data-kind", "number");
        // allow decimals
        inp.step = "any";
        wrap.appendChild(inp);
      } else if (typeof v === "string") {
        const inp = document.createElement("input");
        inp.type = "text";
        inp.className = "admin-appconfig-input";
        inp.id = inputId;
        inp.value = v;
        inp.setAttribute("data-key", k);
        inp.setAttribute("data-kind", "string");
        wrap.appendChild(inp);
      } else {
        // fallback: show JSON for nested values, still editable.
        const inp = document.createElement("textarea");
        inp.className = "admin-db-sql";
        inp.style.minHeight = "80px";
        inp.value = JSON.stringify(v, null, 2);
        inp.setAttribute("data-key", k);
        inp.setAttribute("data-kind", "json");
        wrap.appendChild(inp);
      }

      row.appendChild(labelWrap);
      row.appendChild(wrap);
      fieldsEl.appendChild(row);
    }
  };

  const readFieldsToObject = () => {
    // If not a plain object, just rely on raw JSON editor.
    if (!currentParsed || typeof currentParsed !== "object" || Array.isArray(currentParsed)) return null;
    const out = { ...currentParsed };
    const inputs = fieldsEl.querySelectorAll("[data-key][data-kind]");
    for (const el of inputs) {
      const key = el.getAttribute("data-key");
      const kind = el.getAttribute("data-kind");
      if (!key || !kind) continue;
      if (kind === "bool") {
        out[key] = !!el.checked;
      } else if (kind === "number") {
        const n = Number(el.value);
        if (!Number.isFinite(n)) throw new Error(`Invalid number for "${key}"`);
        out[key] = n;
      } else if (kind === "string") {
        out[key] = String(el.value ?? "");
      } else if (kind === "json") {
        const raw = String(el.value || "").trim();
        out[key] = raw ? JSON.parse(raw) : null;
      }
    }
    return out;
  };

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
      syncHighlight();
      try {
        const parsed = JSON.parse(jsonEl.value || "null");
        renderFields(parsed);
      } catch {
        renderFields(null);
      }
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
      renderFields(parsed);
      setHint("Formatted.");
    } catch (e) {
      setHint(`Invalid JSON: ${e?.message || e}`, true);
    }
  };

  const saveKey = async () => {
    const key = getSelectedKey();
    // Prefer the form editor (fields) when possible.
    let raw = "";
    try {
      const fromFields = readFieldsToObject();
      if (fromFields) {
        raw = JSON.stringify(fromFields, null, 2);
        jsonEl.value = raw; // keep advanced view in sync
        syncHighlight();
      } else {
        raw = (jsonEl.value || "").trim();
      }
    } catch (e) {
      setHint(e?.message ? String(e.message) : `Invalid value: ${e}`, true);
      return;
    }
    if (!raw) {
      setHint("Value is empty.", true);
      return;
    }
    // Validate client-side before sending.
    try {
      JSON.parse(raw);
    } catch (e) {
      setHint(`Invalid JSON: ${e?.message || e}`, true);
      return;
    }
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
        return;
      }
      // Server returns normalized JSON.
      if (typeof payload.value_json === "string") {
        jsonEl.value = payload.value_json;
        syncHighlight();
        try {
          renderFields(JSON.parse(payload.value_json));
        } catch {
          // ignore
        }
      }
      setHint(
        payload?.updated_at_utc
          ? `Saved ${key} · updated ${formatConfigTimestamp(payload.updated_at_utc)}`
          : `Saved ${key}.`,
      );
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, `Save ${key} config`), true);
    } finally {
      saveBtn.disabled = false;
    }
  };

  saveBtn.addEventListener("click", () => void saveKey());
  fmtBtn.addEventListener("click", () => formatJson());
  reloadBtn.addEventListener("click", () => void loadKey());
  keyEl?.addEventListener("change", () => void loadKey());

  jsonEl.addEventListener("input", () => {
    syncHighlight();
    // Keep fields in sync if user edits raw JSON.
    try {
      const parsed = JSON.parse(jsonEl.value || "null");
      renderFields(parsed);
      setHint("");
    } catch {
      // don't spam warnings while typing
    }
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
  syncHighlight();
  void loadKey();
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
    const ok = window.confirm(
      "Restart the poller process?\n\nThis will stop the current poller (if one is running) and start a new one owned by the server.\n\nContinue?",
    );
    if (!ok) return;

    btn.disabled = true;
    setHint("Restarting poller…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/restart-poller", { method: "POST" });
      const pid = payload?.start?.pid ?? payload?.pid;
      setHint(pid ? `Poller restarted (pid ${pid}).` : "Poller restart triggered.");
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Restart poller"), true);
    } finally {
      btn.disabled = false;
    }
  });
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
    const ok = window.confirm(
      "Stop the poller process?\n\nThis stops polling until you restart it.\n\nContinue?",
    );
    if (!ok) return;

    btn.disabled = true;
    setHint("Stopping poller…");
    try {
      await fetchJsonWithInit("/api/admin/stop-poller", { method: "POST" });
      setHint("Poller stopped.");
      appendLocalConsoleLine(pollerLogViewer, { msg: "[admin] Poller stopped." });
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Stop poller"), true);
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
  setupCsvDownload();
  setupDbDownload();
  setupRunDbExport();
  setupClearData();
  setupDeleteSalesTool();
  setupMarketConfigEditor();
  setupStopPoller();
  setupRestartPoller();
  setupMapResize();
  setupLogConsoleWindowControls();
  setupLogSplitView();
  setupEqualHeightConsolePanes();
  refreshLogs();
  if (shouldPollStatsFromStorage()) startStatsPolling();
  else stopStatsPolling();
  refreshVisitors();
  logPollTimer = window.setInterval(refreshLogs, 2500);
  mapPollTimer = window.setInterval(refreshVisitors, 60000);
}

main();
