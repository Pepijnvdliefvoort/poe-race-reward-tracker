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
    m.bindPopup(
      `<strong>${escapeHtml(p.ip)}</strong><br/>Visits: ${p.visits}<br/>${p.lastSeen ? escapeHtml(p.lastSeen) : ""}`,
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
      <td>${v.lastSeen ? escapeHtml(v.lastSeen) : "—"}</td>
      <td>${v.onMap ? "Yes" : "—"}</td>
    </tr>`,
    )
    .join("");
}

let logPollTimer;
let mapPollTimer;

async function refreshLogs() {
  const serverEl = document.getElementById("serverConsole");
  const pollerEl = document.getElementById("pollerConsole");
  try {
    const [serverText, pollerText] = await Promise.all([
      fetchText("/api/admin/logs?stream=server"),
      fetchText("/api/admin/logs?stream=poller"),
    ]);
    if (serverEl) {
      serverEl.textContent = serverText || "(empty)";
      serverEl.scrollTop = serverEl.scrollHeight;
    }
    if (pollerEl) {
      pollerEl.textContent = pollerText || "(empty)";
      pollerEl.scrollTop = pollerEl.scrollHeight;
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
  refreshLogs();
  refreshVisitors();
  logPollTimer = window.setInterval(refreshLogs, 2500);
  mapPollTimer = window.setInterval(refreshVisitors, 60000);
}

main();
