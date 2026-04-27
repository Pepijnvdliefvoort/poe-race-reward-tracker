/**
 * Inferred sales as a scatter: time (x) vs whole-mirror price (y).
 * Compact canvas on the card; a larger view appears on hover (or tap on coarse pointers).
 */
import { getChartTimespanMs } from "../core/state.js";

// Match site accent (gold/orange) used in cards/trends.
const HALF_GRID_COLOR = "rgba(241, 190, 75, 0.14)";
const POINT_COLOR = "rgba(241, 190, 75, 0.92)";

let pluginRegistered = false;

function registerHalfMirrorGridPlugin() {
  if (pluginRegistered) return;
  const C = globalThis.Chart;
  if (!C) return;
  C.register({
    id: "halfMirrorGrid",
    beforeDatasetsDraw(chart) {
      const opt = chart.options?.plugins?.halfMirrorGrid;
      if (!opt?.enabled) return;
      const yS = chart.scales.y;
      if (!yS) return;
      const { top, bottom, left, right } = chart.chartArea;
      if (right <= left || bottom <= top) return;
      const lo = Math.ceil(yS.min);
      const hi = Math.floor(yS.max);
      if (hi - lo > 4) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = opt.color || HALF_GRID_COLOR;
      ctx.setLineDash([3, 5]);
      ctx.lineWidth = 1;
      for (let v = lo; v < hi; v += 1) {
        const y = (yS.getPixelForValue(v) + yS.getPixelForValue(v + 1)) / 2;
        if (y < top || y > bottom) continue;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
      }
      ctx.restore();
    },
  });
  pluginRegistered = true;
}

/** X-axis: calendar day only (no time-of-day). */
function formatAxisDayOnly(ms) {
  const d = new Date(ms);
  const y = d.getFullYear();
  const nowY = new Date().getFullYear();
  if (y !== nowY) {
    return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  }
  return d.toLocaleDateString([], { day: "numeric", month: "short" });
}

/**
 * @param {object} item
 * @param {number} now
 */
function buildViewModel(item, now) {
  const spanMs = getChartTimespanMs();
  const raw = (item.sales || []).filter(
    (s) => s && Number.isFinite(s.time) && Number.isFinite(s.price)
  );
  const inWindow = raw.filter((s) => {
    if (s.time > now) return false;
    if (spanMs === Infinity) return true;
    return s.time >= now - spanMs;
  });
  const points = inWindow.map((s) => ({ x: s.time, y: Number(s.price) }));
  let xMin;
  let xMax = now;
  if (spanMs === Infinity) {
    if (points.length) {
      // Avoid `Math.min(...bigArray)` which can overflow the call stack.
      let minX = Infinity;
      for (let i = 0; i < points.length; i += 1) {
        const v = points[i].x;
        if (v < minX) minX = v;
      }
      xMin = minX;
    } else {
      xMax = now;
      xMin = now - 90 * 24 * 60 * 60 * 1000;
    }
  } else {
    xMin = now - spanMs;
  }
  if (points.length) {
    let tMin = Infinity;
    let tMax = -Infinity;
    for (let i = 0; i < points.length; i += 1) {
      const v = points[i].x;
      if (v < tMin) tMin = v;
      if (v > tMax) tMax = v;
    }
    if (spanMs === Infinity) {
      const pad = Math.max(1, (tMax - tMin) * 0.04, 3 * 60 * 1000);
      xMin = tMin - pad;
      xMax = Math.max(now, tMax + pad);
    } else {
      xMax = now;
    }
  }

  let yMin = 0;
  let yMax = 1;
  if (points.length) {
    let pMin = Infinity;
    let pMax = -Infinity;
    for (let i = 0; i < points.length; i += 1) {
      const v = points[i].y;
      if (v < pMin) pMin = v;
      if (v > pMax) pMax = v;
    }
    // Keep a bit of vertical padding while allowing decimal-valued points.
    const baseMin = Math.max(0, Math.floor(pMin));
    const baseMax = Math.max(baseMin + 1, Math.ceil(pMax));
    const pad = Math.max(0.15, (baseMax - baseMin) * 0.08);
    yMin = Math.max(0, baseMin - pad);
    yMax = baseMax + pad;
  }

  return { points, xMin, xMax, yMin, yMax, hasAnySales: raw.length > 0, inWindow: points.length > 0 };
}

function formatMirrorValue(v) {
  if (v == null || Number.isNaN(v)) return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return "";
  // Prefer integers, but allow decimals when present.
  if (Math.abs(n - Math.round(n)) < 1e-9) return String(Math.round(n));
  return n.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
}

function makeMiniConfig(view) {
  return {
    type: "scatter",
    data: {
      datasets: [
        {
          label: "Sales",
          data: view.points,
          pointRadius: 2,
          pointHoverRadius: 3,
          showLine: false,
          backgroundColor: POINT_COLOR,
          borderColor: POINT_COLOR,
          borderWidth: 0,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      responsive: true,
      animation: false,
      plugins: {
        halfMirrorGrid: { enabled: false },
        legend: { display: false },
        tooltip: { enabled: false },
      },
      scales: {
        x: {
          type: "linear",
          display: false,
          min: view.xMin,
          max: view.xMax,
        },
        y: {
          type: "linear",
          display: false,
          min: view.yMin,
          max: view.yMax,
        },
      },
    },
  };
}

function makeExpandedConfig(view) {
  const ySpan = view.yMax - view.yMin;
  const useHalf = ySpan <= 4;
  return {
    type: "scatter",
    data: {
      datasets: [
        {
          label: "Sale",
          data: view.points,
          pointRadius: 4,
          pointHoverRadius: 6,
          showLine: false,
          backgroundColor: POINT_COLOR,
          borderColor: POINT_COLOR,
          borderWidth: 0,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      responsive: true,
      animation: false,
      layout: { padding: { top: 4, right: 4, left: 0, bottom: 0 } },
      plugins: {
        halfMirrorGrid: { enabled: useHalf, color: HALF_GRID_COLOR },
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {
            label(ctx) {
              const y = ctx.parsed.y;
              const x = ctx.parsed.x;
              if (y == null || x == null) return "";
              return `${formatMirrorValue(y)} mirror · ${new Date(x).toLocaleString()}`;
            },
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          min: view.xMin,
          max: view.xMax,
          afterTickToLabelConversion(scale) {
            if (scale.id !== "x" || !scale.ticks) return;
            const seen = new Set();
            for (const t of scale.ticks) {
              // Chart.js internals expect tick labels to be strings (or string arrays).
              // Some versions/plugins can produce non-string labels; normalize to avoid
              // `startsWith` crashes inside the UMD bundle.
              if (!t || t.value == null) continue;
              const d = new Date(t.value);
              const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
              if (seen.has(key)) {
                t.label = "";
              } else {
                seen.add(key);
                if (t.label != null && typeof t.label !== "string") {
                  t.label = String(t.label);
                }
              }
            }
          },
          grid: { color: "rgba(150, 170, 200, 0.12)" },
          ticks: {
            maxRotation: 0,
            maxTicksLimit: 8,
            color: "rgba(200, 214, 235, 0.75)",
            font: { size: 10 },
            callback(v) {
              if (v == null || !Number.isFinite(v)) return "";
              return formatAxisDayOnly(v);
            },
          },
        },
        y: {
          min: view.yMin,
          max: view.yMax,
          grid: { color: "rgba(150, 170, 200, 0.12)" },
          ticks: {
            stepSize: 1,
            color: "rgba(200, 214, 235, 0.78)",
            font: { size: 10 },
          },
        },
      },
    },
  };
}

let salesChartCounter = 0;

function getNextId() {
  salesChartCounter += 1;
  return `sales-chart-${salesChartCounter}`;
}

export function ensureSalesChartDom(entry) {
  if (entry.salesChartOuter) return;
  const outer = document.createElement("div");
  outer.className = "sales-chart-outer";
  outer.setAttribute("aria-label", "Sales over time, click the chart strip to expand");
  const hintId = getNextId();
  outer.setAttribute("aria-describedby", hintId);

  const mini = document.createElement("div");
  mini.className = "sales-chart-mini";
  mini.setAttribute("role", "button");
  mini.tabIndex = 0;
  mini.setAttribute("aria-label", "Open sales chart");
  const miniCanvas = document.createElement("canvas");
  miniCanvas.className = "sales-chart-canvas";
  miniCanvas.dataset.cardKey = entry.card?.dataset?.cardKey || "";
  miniCanvas.dataset.lazyChart = "sales";
  mini.appendChild(miniCanvas);

  const pop = document.createElement("div");
  pop.className = "sales-chart-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-modal", "false");
  pop.setAttribute("aria-labelledby", hintId);

  const header = document.createElement("div");
  header.className = "sales-chart-pop-header";
  const sub = document.createElement("p");
  sub.className = "sales-chart-sub";
  sub.id = hintId;
  sub.textContent = "Sales (mirrors)";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "sales-chart-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.textContent = "×";
  header.append(sub, closeBtn);

  const plot = document.createElement("div");
  plot.className = "sales-chart-plot";
  const popCanvas = document.createElement("canvas");
  popCanvas.className = "sales-chart-canvas sales-chart-canvas-expanded";

  const empty = document.createElement("div");
  empty.className = "sales-chart-empty";
  empty.setAttribute("hidden", "");

  plot.append(popCanvas, empty);
  pop.append(header, plot);
  outer.append(mini, pop);
  // Place sales chart BELOW the "Est. sold" line (if present),
  // so the summary stays above this chart strip.
  if (entry.salesSummary) {
    entry.salesSummary.insertAdjacentElement("afterend", outer);
  } else if (entry.chartWrap) {
    entry.chartWrap.insertAdjacentElement("afterend", outer);
  } else if (entry.card) {
    entry.card.appendChild(outer);
  }

  entry.salesChartOuter = outer;
  entry.salesChartMini = mini;
  entry.salesChartMiniCanvas = miniCanvas;
  entry.salesChartPop = pop;
  entry.salesChartPopCanvas = popCanvas;
  entry.salesChartEmpty = empty;
  entry.salesCloseBtn = closeBtn;
  entry.salesPopBackdrop = null;
  entry._salesPopMounted = false;
  entry._salesKeydown = null;
  entry.salesChart = null;
  entry.salesChartExpanded = null;
  entry.salesView = null;
  entry._salesOpen = false;
  entry._salesDocClick = null;

  const onClose = (e) => {
    e?.preventDefault?.();
    e?.stopPropagation?.();
    e?.stopImmediatePropagation?.();
    closeSalesPop(entry);
  };
  closeBtn.addEventListener("click", onClose, { capture: true });
  wireSalesChartInteractions(entry, mini);
}

function setEmptyVisible(entry, on, message) {
  if (!entry.salesChartEmpty) return;
  if (on) {
    entry.salesChartEmpty.hidden = false;
    entry.salesChartEmpty.textContent = message || "";
  } else {
    entry.salesChartEmpty.hidden = true;
    entry.salesChartEmpty.textContent = "";
  }
}

function ensureSalesBackdrop(entry) {
  if (entry.salesPopBackdrop) return;
  const b = document.createElement("div");
  b.className = "sales-chart-backdrop";
  b.hidden = true;
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    closeSalesPop(entry);
  });
  entry.salesPopBackdrop = b;
}

function mountSalesPop(entry) {
  if (entry._salesPopMounted) {
    return;
  }
  ensureSalesBackdrop(entry);
  const { salesPopBackdrop: bd, salesChartPop, salesChartOuter: outer } = entry;
  bd.hidden = false;
  salesChartPop.classList.add("sales-chart-pop--floating");
  document.body.append(bd, salesChartPop);
  entry._salesPopMounted = true;
  outer.classList.add("is-open");
}

function unmountSalesPop(entry) {
  if (!entry._salesPopMounted) {
    return;
  }
  const { salesChartPop, salesPopBackdrop, salesChartOuter: outer, salesChartMini: mini } = entry;
  if (salesPopBackdrop) {
    salesPopBackdrop.hidden = true;
    if (salesPopBackdrop.parentNode) {
      salesPopBackdrop.remove();
    }
  }
  if (salesChartPop) {
    salesChartPop.classList.remove("sales-chart-pop--floating");
    if (outer) {
      outer.append(mini, salesChartPop);
    }
  }
  entry._salesPopMounted = false;
  outer.classList.remove("is-open");
}

function openSalesPop(entry) {
  if (entry._salesOpen) {
    if (entry.salesChartExpanded) {
      requestAnimationFrame(() => {
        entry.salesChartExpanded.resize();
      });
    }
    return;
  }
  const v = entry.salesView || entry.pendingSalesView;
  if (!v) {
    return;
  }
  entry.salesView = v;
  entry._salesOpen = true;
  registerHalfMirrorGridPlugin();
  mountSalesPop(entry);
  if (!entry.salesChartExpanded) {
    const canvas = entry.salesChartPopCanvas;
    entry.salesChartExpanded = new globalThis.Chart(canvas.getContext("2d"), makeExpandedConfig(v));
  } else {
    const ec = entry.salesChartExpanded;
    ec.data.datasets[0].data = v.points;
    ec.options.scales.x.min = v.xMin;
    ec.options.scales.x.max = v.xMax;
    ec.options.scales.y.min = v.yMin;
    ec.options.scales.y.max = v.yMax;
    const ySpan = v.yMax - v.yMin;
    ec.options.plugins = ec.options.plugins || {};
    ec.options.plugins.halfMirrorGrid = { enabled: ySpan <= 4, color: HALF_GRID_COLOR };
    ec.update();
  }
  requestAnimationFrame(() => {
    if (entry.salesChartExpanded) {
      entry.salesChartExpanded.resize();
    }
  });

  if (!entry._salesKeydown) {
    const onKey = (e) => {
      if (e.key === "Escape" && entry._salesOpen) {
        closeSalesPop(entry);
      }
    };
    document.addEventListener("keydown", onKey, true);
    entry._salesKeydown = onKey;
  }
}

function closeSalesPop(entry) {
  if (!entry._salesOpen) return;
  entry._salesOpen = false;
  unmountSalesPop(entry);
  // The expanded popover is portaled into/out of <body>. Keeping the same Chart.js instance
  // across unmount/remount can confuse internal observers/layout and can surface as
  // `t.startsWith is not a function` / stack overflows on the *second* open.
  // Recreate the expanded chart each time to keep Chart.js in a clean state.
  if (entry.salesChartExpanded) {
    entry.salesChartExpanded.destroy();
    entry.salesChartExpanded = null;
  }
  if (entry._salesKeydown) {
    document.removeEventListener("keydown", entry._salesKeydown, true);
    entry._salesKeydown = null;
  }
}

function wireSalesChartInteractions(entry, mini) {
  mini.addEventListener("click", (e) => {
    e.stopPropagation();
    if (entry._salesOpen) {
      return;
    }
    if (!entry.salesView && !entry.pendingSalesView) {
      return;
    }
    openSalesPop(entry);
  });

  mini.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    if (entry._salesOpen) {
      return;
    }
    if (!entry.salesView && !entry.pendingSalesView) {
      return;
    }
    openSalesPop(entry);
  });
}

/**
 * @param {object} cardEntry
 */
export function ensureSalesChart(cardEntry) {
  if (cardEntry.salesChart || !cardEntry.salesChartMiniCanvas) return;
  const view = cardEntry.pendingSalesView;
  if (!view) return;
  cardEntry.salesView = view;
  registerHalfMirrorGridPlugin();
  const canvas = cardEntry.salesChartMiniCanvas;
  cardEntry.salesChart = new globalThis.Chart(canvas.getContext("2d"), makeMiniConfig(cardEntry.salesView));
}

export function applyPendingSalesChartUpdate(cardEntry) {
  if (!cardEntry.pendingSalesView) return;
  cardEntry.salesView = cardEntry.pendingSalesView;
  const v = cardEntry.salesView;

  if (cardEntry.salesChart) {
    const c = cardEntry.salesChart;
    c.data.datasets[0].data = v.points;
    c.options.scales.x.min = v.xMin;
    c.options.scales.x.max = v.xMax;
    c.options.scales.y.min = v.yMin;
    c.options.scales.y.max = v.yMax;
    c.update();
  }

  if (cardEntry.salesChartExpanded) {
    const ec = cardEntry.salesChartExpanded;
    ec.data.datasets[0].data = v.points;
    ec.options.scales.x.min = v.xMin;
    ec.options.scales.x.max = v.xMax;
    ec.options.scales.y.min = v.yMin;
    ec.options.scales.y.max = v.yMax;
    const ySpan = v.yMax - v.yMin;
    ec.options.plugins = ec.options.plugins || {};
    ec.options.plugins.halfMirrorGrid = { enabled: ySpan <= 4, color: HALF_GRID_COLOR };
    ec.update();
    if (cardEntry._salesOpen) {
      requestAnimationFrame(() => ec.resize());
    }
  }

  if (v.inWindow) {
    setEmptyVisible(cardEntry, false);
  } else if (v.hasAnySales) {
    setEmptyVisible(cardEntry, true, "No sales in the current chart time window");
  } else {
    setEmptyVisible(cardEntry, true, "No sales recorded");
  }
}

/**
 * @param {object} cardEntry
 * @param {object} item
 * @param {number} [now]
 */
export function buildPendingSalesView(cardEntry, item, now = Date.now()) {
  if (!item || !item.itemName) return;
  if (cardEntry.salesChartOuter) {
    cardEntry.salesChartOuter.hidden = false;
  }
  const view = buildViewModel(item, now);
  cardEntry.pendingSalesView = view;
}

export function destroySalesChart(cardEntry) {
  if (cardEntry._salesOpen) {
    closeSalesPop(cardEntry);
  }
  if (cardEntry.salesChart) {
    cardEntry.salesChart.destroy();
    cardEntry.salesChart = null;
  }
  if (cardEntry.salesChartExpanded) {
    cardEntry.salesChartExpanded.destroy();
    cardEntry.salesChartExpanded = null;
  }
  if (cardEntry._salesDocClick) {
    document.removeEventListener("click", cardEntry._salesDocClick, true);
    cardEntry._salesDocClick = null;
  }
  if (cardEntry._salesKeydown) {
    document.removeEventListener("keydown", cardEntry._salesKeydown, true);
    cardEntry._salesKeydown = null;
  }
}
