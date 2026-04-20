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

async function fetchJsonWithInit(path, init) {
  const res = await fetch(path, { ...fetchOpts, ...(init || {}) });
  if (res.status === 403) {
    const err = new Error("Forbidden");
    err.status = 403;
    throw err;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
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

function setupLogConsoleWindowControls() {
  const splitEl = document.getElementById("adminLogSplit");
  const handle = document.getElementById("adminLogSplitHandle");
  const leftPane = document.getElementById("serverConsolePane");
  const rightPane = document.getElementById("pollerConsolePane");
  const emptyEl = document.getElementById("adminLogEmpty");
  const taskbarEl = document.getElementById("adminLogTaskbar");
  if (!splitEl || !handle || !leftPane || !rightPane || !emptyEl || !taskbarEl) return;

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
      setPollerHint(
        e.status === 403
          ? "Unauthorized. Open /admin?token=… once using the ADMIN_TOKEN from the server (GitHub Actions secret), then this page will set a cookie."
          : `Stop poller: ${e.message || e}`,
        true,
      );
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
      setPollerHint(
        e.status === 403
          ? "Unauthorized. Open /admin?token=… once using the ADMIN_TOKEN from the server (GitHub Actions secret), then this page will set a cookie."
          : `Start poller: ${e.message || e}`,
        true,
      );
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

  const labelFor = (consoleKey) => (consoleKey === "server" ? "server.log" : "poller.log");

  const dotColorFor = (consoleKey) => (consoleKey === "server" ? "#28c840" : "#febc2e");

  const setMode = (consoleKey, mode) => {
    state[consoleKey].mode = mode;
    saveState();
    render();
  };

  const closeConsole = (consoleKey) => setMode(consoleKey, "closed");
  const minimizeConsole = (consoleKey) => setMode(consoleKey, "minimized");

  const restoreConsole = (consoleKey) => {
    // If both are closed, restoring should just open one.
    if (isClosed("server") && isClosed("poller")) {
      state.server.mode = consoleKey === "server" ? "open" : "closed";
      state.poller.mode = consoleKey === "poller" ? "open" : "closed";
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
    const otherKey = consoleKey === "server" ? "poller" : "server";
    state[consoleKey].mode = "open";
    // Requirement: maximize current console, minimizing the other one.
    if (!isClosed(otherKey)) state[otherKey].mode = "minimized";
    saveState();
    render();
  };

  const renderTaskbar = async () => {
    taskbarEl.innerHTML = "";
    // Always-visible "dock" with fixed slots for each console.
    const allKeys = ["server", "poller"];
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
    };
  };

  const renderLayout = async () => {
    const leftVisible = isVisible("server");
    const rightVisible = isVisible("poller");

    // Show/hide panes based on mode.
    await Promise.all([
      animatePaneVisibility(leftPane, leftVisible, { toEl: dockTargets?.server, fromEl: dockTargets?.server }),
      animatePaneVisibility(rightPane, rightVisible, { toEl: dockTargets?.poller, fromEl: dockTargets?.poller }),
    ]);

    // Show placeholder if none visible.
    const anyVisible = leftVisible || rightVisible;
    await animateAuxVisibility(emptyEl, "admin-console-empty--anim-hide", !anyVisible);
    if (!anyVisible) {
      emptyEl.innerHTML = "";
      const msg = document.createElement("div");
      msg.textContent = "No console active";
      emptyEl.appendChild(msg);
    }

    // Handle + split ratio only makes sense when both are visible and desktop layout.
    const bothVisible = leftVisible && rightVisible;
    handle.style.display = bothVisible ? "" : "none";

    // If only one visible, make it take full width.
    if (leftVisible && !rightVisible) {
      leftPane.style.flex = "1 1 0";
      rightPane.style.flex = "";
    } else if (!leftVisible && rightVisible) {
      rightPane.style.flex = "1 1 0";
      leftPane.style.flex = "";
    }
  };

  let renderToken = 0;
  let dockTargets = { server: null, poller: null };
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
    if (consoleKey !== "server" && consoleKey !== "poller") return;
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
    getVisiblePair: () => ({ server: isVisible("server"), poller: isVisible("poller") }),
    render,
  };
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
    // Only apply ratio when both panes are visible (otherwise the single pane should be full-width).
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (vis && (!vis.server || !vis.poller)) return;
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
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (vis && (!vis.server || !vis.poller)) return;
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
    const vis = window.__adminLogWindowState?.getVisiblePair?.();
    if (vis && (!vis.server || !vis.poller)) return;
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
      setHint(
        e.status === 403
          ? "Unauthorized. Open /admin?token=… once using the ADMIN_TOKEN from the server (GitHub Actions secret), then this page will set a cookie."
          : `Clear data: ${e.message || e}`,
        true,
      );
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
      setHint(
        e.status === 403
          ? "Unauthorized. Open /admin?token=… once using the ADMIN_TOKEN from the server (GitHub Actions secret), then this page will set a cookie."
          : `Restart poller: ${e.message || e}`,
        true,
      );
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
      setHint(
        e.status === 403
          ? "Unauthorized. Open /admin?token=… once using the ADMIN_TOKEN from the server (GitHub Actions secret), then this page will set a cookie."
          : `Stop poller: ${e.message || e}`,
        true,
      );
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
  refreshVisitors();
  logPollTimer = window.setInterval(refreshLogs, 2500);
  mapPollTimer = window.setInterval(refreshVisitors, 60000);
}

main();
