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
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
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
    </tr>`,
    )
    .join("");
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

  const prefersReducedMotion = () => !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r()));

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
      return;
    }
    setMode(consoleKey, "open");
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

      if (k === "poller") {
        const actions = document.createElement("span");
        actions.className = "admin-console-dock-actions";

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

        actions.appendChild(stopBtn);
        actions.appendChild(restartBtn);
        btn.appendChild(actions);
      }

      btn.addEventListener("click", () => toggleFromDock(k));
      taskbarEl.appendChild(btn);
    });

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
    handleA.style.display = serverVisible && pollerVisible ? "" : "none";
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
    const wA = vis?.server && vis?.poller ? _outerWidth(handleA) : 0;
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
    // If poller is closed but the other two are open, split remaining width evenly.
    if (vis && vis.server && !vis.poller && vis.stats) {
      serverPane.style.flex = "1 1 0";
      pollerPane.style.flex = "";
      statsPane.style.flex = "1 1 0";
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
      if (vis && (!vis.server || !vis.poller)) return;
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
      if (vis && (!vis.server || !vis.poller)) return;
    } else {
      if (vis && (!vis.poller || !vis.stats)) return;
    }
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    const delta = ev.key === "ArrowLeft" ? -step : step;
    if (which === "a") {
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
let logRefreshSeq = 0;

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

  const cards = [
    { key: "cpu", k: "CPU", v: `${formatPercent(cpu.percent)}` },
    { key: "uptime", k: "Uptime", v: formatUptimeFromBootMs(payload?.system?.bootTimeMs) },
    { key: "ram", k: "RAM", v: `${formatPercent(mem.usedPercent)} · ${formatBytesMb(mem.usedMb)} / ${formatBytesMb(mem.totalMb)}` },
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
}

async function refreshStats() {
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
    window.location.href = "/api/admin/download/price_poll.csv";
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
      "This will clear:\n\n- web/listings_cache.json (deleted)\n- price_poll.csv (keeps the header row)\n\nContinue?",
    );
    if (!ok) return;

    btn.disabled = true;
    setHint("Clearing data…");
    try {
      const payload = await fetchJsonWithInit("/api/admin/clear-data", { method: "POST" });
      const cleared = payload?.cleared || {};
      const csv = cleared.pricePollCsv ? "price_poll.csv" : null;
      const cache = cleared.listingsCache ? "listings_cache.json" : null;
      const names = [csv, cache].filter(Boolean).join(", ");
      setHint(names ? `Cleared: ${names}` : "Cleared.");
    } catch (e) {
      setHint(adminEndpointErrorMessage(e, "Clear data"), true);
    } finally {
      btn.disabled = false;
    }
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
  setupClearData();
  setupStopPoller();
  setupRestartPoller();
  setupMapResize();
  setupLogConsoleWindowControls();
  setupLogSplitView();
  refreshLogs();
  if (shouldPollStatsFromStorage()) startStatsPolling();
  else stopStatsPolling();
  refreshVisitors();
  logPollTimer = window.setInterval(refreshLogs, 2500);
  mapPollTimer = window.setInterval(refreshVisitors, 60000);
}

main();
