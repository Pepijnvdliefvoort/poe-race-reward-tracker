import {
    chartMap,
    dom,
    getChartTimespanMs,
    MAX_ACTUAL_POINTS,
    PREDICTION_POINTS,
    THREE_MONTHS_MS,
    saveFavorites,
    state,
} from "../core/state.js";
import { aggregateInferenceSignalsOverWindow, formatEstimatedSoldLine } from "../domain/inferenceStats.js";
import {
  getDisplayHighestMirror,
  getDisplayLowestMirror,
  isShowingLastKnownMirrorPrice,
} from "../domain/pricing.js";
import { formatNumber, formatTime, getChartSeriesWithPrediction } from "../core/utils.js";
import { stopListingsPopover, wireListingsPopover } from "../cards/listingsPopover.js";
import {
    applyPendingSalesChartUpdate,
    buildPendingSalesView,
    destroySalesChart,
    ensureSalesChart,
    ensureSalesChartDom,
} from "./itemSalesChart.js";

const IMG_PLACEHOLDER_SRC =
  "data:image/svg+xml;charset=utf-8," +
  encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"></svg>');

let cardImageObserver = null;
let cardChartObserver = null;
let cardViewportObserver = null;

function ensureCardViewportObserver() {
  if (cardViewportObserver) return cardViewportObserver;
  if (!("IntersectionObserver" in window)) return null;

  // Overscan so cards are "warmed up" before they scroll into view.
  cardViewportObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const cardEl = entry.target;
        const key = cardEl?.dataset?.cardKey;
        if (!key) continue;
        const cardEntry = chartMap.get(key);
        if (!cardEntry) continue;

        cardEntry.isNearViewport = !!entry.isIntersecting;

        // If we skipped updates while offscreen, apply the latest state when it becomes visible.
        if (cardEntry.isNearViewport && cardEntry.pendingItemForViewport) {
          const { item, onFavoriteToggle } = cardEntry.pendingItemForViewport;
          cardEntry.pendingItemForViewport = null;
          updateCard(item, onFavoriteToggle);
        }
      }
    },
    { root: null, rootMargin: "900px 0px", threshold: 0.01 },
  );

  return cardViewportObserver;
}

function ensureCardImageObserver() {
  if (cardImageObserver) return cardImageObserver;
  if (!("IntersectionObserver" in window)) return null;

  cardImageObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        const lazySrc = img?.dataset?.lazySrc;
        if (lazySrc) {
          img.src = lazySrc;
        }
        cardImageObserver.unobserve(img);
      }
    },
    // Start loading just before it scrolls into view.
    { root: null, rootMargin: "250px 0px", threshold: 0.01 }
  );

  return cardImageObserver;
}

function ensureCardChartObserver() {
  if (cardChartObserver) return cardChartObserver;
  if (!("IntersectionObserver" in window)) return null;

  cardChartObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const canvas = entry.target;
        const key = canvas?.dataset?.cardKey;
        if (!key) continue;
        const cardEntry = chartMap.get(key);
        if (!cardEntry) continue;
        const kind = canvas?.dataset?.lazyChart || "price";
        if (kind === "sales") {
          ensureSalesChart(cardEntry);
          applyPendingSalesChartUpdate(cardEntry);
        } else {
          ensureChart(cardEntry);
          applyPendingChartUpdate(cardEntry);
        }
        cardChartObserver.unobserve(canvas);
      }
    },
    { root: null, rootMargin: "500px 0px", threshold: 0.01 }
  );

  return cardChartObserver;
}

function createChart(canvas) {
  ensureTooltipOffsetPositioner();

  return new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "Mirror price",
          data: [],
          borderColor: "#f8b400",
          backgroundColor: "rgba(248, 180, 0, 0.18)",
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 3,
          pointBorderWidth: 0,
          pointBackgroundColor: "#f8b400",
          tension: 0.24,
          spanGaps: true,
          fill: false,
        },
        {
          label: "Prediction",
          data: [],
          borderColor: "rgba(180, 180, 180, 0.9)",
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 0,
          borderDash: [2, 2],
          tension: 0.24,
          spanGaps: true,
          fill: false,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      responsive: true,
      animation: false,
      plugins: {
        legend: {
          display: false,
        },
        tooltip: {
          // Single tooltip box; we decide which lines to show via callbacks.
          callbacks: {
            title: () => null,
            label: (ctx) => {
              const y = ctx?.parsed?.y;
              if (y == null || Number.isNaN(y)) return null;
              // Hide the "connector" value for the prediction series (it equals the last actual point),
              // while keeping it in the dataset so the dotted line visually starts from the last point.
              if (ctx.datasetIndex === 1) {
                const data = ctx?.dataset?.data || [];
                const idx = ctx.dataIndex ?? -1;
                const prev = idx > 0 ? data[idx - 1] : null;
                if (prev == null) return null;
              }

              const tag = ctx.datasetIndex === 1 ? "Pred" : "Price";
              return `${tag}: ${Math.round(y)} mirrors`;
            },
          },
          enabled: true,
          mode: "index",
          intersect: false,
          axis: "x",
          displayColors: false,
          position: "offsetPoint",
          xAlign: "center",
          yAlign: "bottom",
          caretPadding: 0,
          caretSize: 0,
          padding: { left: 8, right: 8, top: 6, bottom: 6 },
          cornerRadius: 8,
          backgroundColor: "rgba(0, 0, 0, 0.78)",
          borderColor: "rgba(255, 255, 255, 0.12)",
          borderWidth: 1,
          bodySpacing: 0,
          titleMarginBottom: 0,
          titleColor: "rgba(255, 255, 255, 0.9)",
          bodyColor: "rgba(255, 255, 255, 0.86)",
          titleFont: { size: 11, weight: "600" },
          bodyFont: { size: 11, weight: "500" },
        },
      },
      scales: {
        x: {
          display: false,
        },
        y: {
          display: false,
        },
      },
      elements: {
        line: {
          capBezierPoints: true,
        },
      },
    },
  });
}

function ensureChart(cardEntry) {
  if (cardEntry.chart) return cardEntry.chart;
  if (!cardEntry.canvas) return null;
  const chart = createChart(cardEntry.canvas);
  cardEntry.chart = chart;
  cardEntry.chartTooltipCleanup = wireMobileTooltipDismiss(chart);
  return chart;
}

function applyPendingChartUpdate(cardEntry) {
  if (!cardEntry?.chart || !cardEntry?.pendingChart) return;
  const { labels, actualSeries, predictionSeries } = cardEntry.pendingChart;
  cardEntry.chart.data.labels = labels;
  cardEntry.chart.data.datasets[0].data = actualSeries;
  cardEntry.chart.data.datasets[1].data = predictionSeries;
  cardEntry.chart.update();
}

function setCardImage(img, src) {
  if (!img) return;

  // Always set these to enable native lazy-loading where supported.
  img.loading = "lazy";
  img.decoding = "async";
  img.fetchPriority = "low";

  if (!src) {
    img.dataset.lazySrc = "";
    img.src = IMG_PLACEHOLDER_SRC;
    img.classList.remove("is-loaded");
    img.classList.add("is-loading");
    img.style.display = "none";
    return;
  }

  img.style.display = "block";

  // Avoid resetting when unchanged (prevents flicker during refresh cycles).
  const current = img.dataset.currentSrc || img.currentSrc || img.src;
  if (current === src) {
    return;
  }

  img.dataset.currentSrc = src;
  img.dataset.lazySrc = src;
  img.classList.remove("is-loaded");
  img.classList.add("is-loading");

  const observer = ensureCardImageObserver();
  if (observer) {
    // Keep a cheap placeholder in the DOM to avoid loading all images at once.
    if (!img.src || img.src === window.location.href) {
      img.src = IMG_PLACEHOLDER_SRC;
    } else if (img.src !== IMG_PLACEHOLDER_SRC) {
      img.src = IMG_PLACEHOLDER_SRC;
    }
    observer.observe(img);
  } else {
    // Fallback: let the browser handle lazy-loading if possible.
    img.src = src;
  }
}

function ensureTooltipOffsetPositioner() {
  const tooltip = Chart?.Tooltip;
  if (!tooltip?.positioners) return;
  if (tooltip.positioners.offsetPoint) return;

  tooltip.positioners.offsetPoint = (items, eventPosition) => {
    const base = tooltip.positioners.nearest(items, eventPosition);
    if (!base) return false;
    // Shift the tooltip a bit so the hovered point stays visible.
    return { x: base.x, y: base.y - 14 };
  };
}

function clearChartTooltip(chart) {
  if (!chart) return;
  const tooltip = chart.tooltip;
  if (!tooltip) return;

  // Chart.js v3/v4: clearing active elements hides the tooltip.
  if (typeof tooltip.setActiveElements === "function") {
    tooltip.setActiveElements([], { x: 0, y: 0 });
  }
  chart.update("none");
}

function wireMobileTooltipDismiss(chart) {
  // Only do this for coarse pointers (phones/tablets). On desktop, hover tooltips
  // should remain stable while scrolling with a mouse wheel.
  if (!window.matchMedia?.("(pointer: coarse)").matches) {
    return () => {};
  }

  let lastDismissAt = 0;
  const dismiss = () => {
    // Avoid doing work for every scroll tick.
    const now = performance.now();
    if (now - lastDismissAt < 50) return;
    lastDismissAt = now;
    clearChartTooltip(chart);
  };

  // `scroll` fires after scrolling begins; `touchmove` catches the gesture early.
  window.addEventListener("scroll", dismiss, { passive: true, capture: true });
  window.addEventListener("touchmove", dismiss, { passive: true, capture: true });
  window.addEventListener("touchcancel", dismiss, { passive: true, capture: true });
  window.addEventListener("pointercancel", dismiss, { passive: true, capture: true });

  return () => {
    window.removeEventListener("scroll", dismiss, { capture: true });
    window.removeEventListener("touchmove", dismiss, { capture: true });
    window.removeEventListener("touchcancel", dismiss, { capture: true });
    window.removeEventListener("pointercancel", dismiss, { capture: true });
  };
}

export function ensureCard(item, onFavoriteToggle) {
  const key = item.itemName;
  let entry = chartMap.get(key);

  if (entry) {
    return entry;
  }

  const card = document.createElement("article");
  card.className = "card card-enter";
  card.dataset.cardKey = key;
  card.addEventListener(
    "animationend",
    () => {
      card.classList.remove("card-enter");
    },
    { once: true }
  );

  const favoriteBtn = document.createElement("button");
  favoriteBtn.type = "button";
  favoriteBtn.className = "favorite-toggle";
  favoriteBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    if (state.favoriteItems.has(key)) {
      state.favoriteItems.delete(key);
    } else {
      state.favoriteItems.add(key);
    }
    saveFavorites();
    onFavoriteToggle();
  });

  const title = document.createElement("h2");
  title.textContent = key;

  const artFrame = document.createElement("div");
  artFrame.className = "art-frame";

  const img = document.createElement("img");
  img.className = "item-art";
  img.alt = `${key} art`;
  img.style.cursor = "pointer";
  img.src = IMG_PLACEHOLDER_SRC;
  img.classList.add("is-loading");
  img.addEventListener("load", () => {
    // Placeholder also triggers load; only mark loaded when we have a real src.
    if (!img.dataset?.currentSrc) return;
    if (img.currentSrc === IMG_PLACEHOLDER_SRC || img.src === IMG_PLACEHOLDER_SRC) return;
    img.classList.remove("is-loading");
    img.classList.add("is-loaded");
  });
  img.addEventListener("error", () => {
    // Don't keep retrying a bad URL; keep placeholder.
    img.classList.remove("is-loaded");
    img.classList.add("is-loading");
    img.src = IMG_PLACEHOLDER_SRC;
  });

  artFrame.append(img);

  const priceBox = document.createElement("div");
  priceBox.className = "price-box";

  const chartWrap = document.createElement("div");
  chartWrap.className = "chart-wrap";
  const canvas = document.createElement("canvas");
  canvas.dataset.cardKey = key;
  canvas.dataset.lazyChart = "price";
  chartWrap.appendChild(canvas);

  const salesSummary = document.createElement("div");
  salesSummary.className = "card-sales-summary";
  salesSummary.setAttribute("aria-label", "Estimated number of sales over chart filter window");
  salesSummary.hidden = true;

  const trend = document.createElement("div");
  trend.className = "trend";
  const trendLabel = document.createElement("span");
  trendLabel.className = "trend-listings";
  trendLabel.textContent = "Price Trend: ";
  const trendIndicator = document.createElement("span");
  const listingsHoverArea = document.createElement("div");
  listingsHoverArea.className = "listings-hover-area";
  listingsHoverArea.tabIndex = 0;
  listingsHoverArea.setAttribute("role", "button");
  listingsHoverArea.setAttribute("aria-label", "Show listing details");

  const trendListings = document.createElement("span");
  trendListings.className = "trend-listings listings-count";

  const listingsPopover = document.createElement("div");
  listingsPopover.className = "listings-popover";

  const listingsPopoverHeader = document.createElement("div");
  listingsPopoverHeader.className = "listings-popover-header";
  listingsPopoverHeader.textContent = "Listings";

  const listingsPopoverSubline = document.createElement("div");
  listingsPopoverSubline.className = "listings-popover-subline";

  const listingsPopoverBody = document.createElement("div");
  listingsPopoverBody.className = "listings-popover-body";
  listingsPopoverBody.textContent = "Hover to load listing details.";

  const listingsPopoverPlaceholder = document.createElement("span");
  listingsPopoverPlaceholder.className = "listings-popover-placeholder";
  listingsPopoverPlaceholder.style.display = "none";

  listingsPopover.append(listingsPopoverHeader, listingsPopoverSubline, listingsPopoverBody);
  listingsHoverArea.append(trendListings, listingsPopover, listingsPopoverPlaceholder);
  trend.append(trendLabel, trendIndicator, listingsHoverArea);

  artFrame.prepend(favoriteBtn);
  card.append(title, artFrame, priceBox, chartWrap, salesSummary, trend);
  dom.cardsEl.appendChild(card);

  entry = {
    card,
    favoriteBtn,
    img,
    artFrame,
    priceBox,
    chartWrap,
    canvas,
    salesSummary,
    trend,
    trendIndicator,
    trendListings,
    listingsHoverArea,
    listingsPopover,
    listingsPopoverHome: listingsHoverArea,
    listingsPopoverPlaceholder,
    listingsPopoverMountedToBody: false,
    listingsPopoverPositionLocked: false,
    listingsPopoverHeader,
    listingsPopoverBody,
    listingsPopoverSubline,
    chart: null,
    pendingChart: null,
    chartTooltipCleanup: null,
    currentQueryId: null,
    loadedQueryId: null,
    loadingQueryId: null,
    listingsRefreshTimer: null,
    handleViewportChange: null,
    openPopover: null,
    closePopover: null,
    isNearViewport: true,
    pendingItemForViewport: null,
    salesChartOuter: null,
    salesChartMini: null,
    salesChartMiniCanvas: null,
    salesChartPop: null,
    salesChartPopCanvas: null,
    salesChartEmpty: null,
    salesChart: null,
    salesChartExpanded: null,
    pendingSalesView: null,
    salesView: null,
    _salesOpen: null,
    _salesDocClick: null,
  };
  chartMap.set(key, entry);
  ensureSalesChartDom(entry);
  wireListingsPopover(entry);

  const viewportObserver = ensureCardViewportObserver();
  viewportObserver?.observe?.(card);
  return entry;
}

export function updateCard(item, onFavoriteToggle) {
  const entry = ensureCard(item, onFavoriteToggle);
  const {
    card,
    favoriteBtn,
    img,
    artFrame,
    priceBox,
    salesSummary,
    trend,
    trendIndicator,
    trendListings,
    listingsHoverArea,
    listingsPopoverHeader,
    listingsPopoverBody,
    listingsPopoverSubline,
    canvas,
  } = entry;
  const cutoff = Date.now() - getChartTimespanMs();
  const rawPoints = (item.points || []).filter((p) => p.time >= cutoff);
  const { actual, predicted } = getChartSeriesWithPrediction(rawPoints, MAX_ACTUAL_POINTS, PREDICTION_POINTS);
  const chartPoints = [...actual, ...predicted];
  const sparkValues = actual.map((p) => p.y);

  card.classList.toggle("next-in-line", item.itemName === state.nextInLineItemName);
  const isFavorited = state.favoriteItems.has(item.itemName);
  card.classList.toggle("favorited", isFavorited);
  favoriteBtn.classList.toggle("checked", isFavorited);
  favoriteBtn.textContent = isFavorited ? "★" : "☆";
  favoriteBtn.setAttribute("aria-label", isFavorited ? `Unfavorite ${item.itemName}` : `Favorite ${item.itemName}`);
  favoriteBtn.title = isFavorited ? "Unfavorite" : "Favorite";

  const labels = chartPoints.map((p) => formatTime(p.x));

  const actualCount = actual.length;
  const totalCount = chartPoints.length;
  const actualSeries = new Array(totalCount).fill(null);
  for (let i = 0; i < actualCount; i += 1) {
    actualSeries[i] = actual[i].y != null ? Math.round(actual[i].y) : null;
  }

  const predictionSeries = new Array(totalCount).fill(null);
  if (predicted.length > 0 && actualCount > 0) {
    const lastActualIndex = actualCount - 1;
    predictionSeries[lastActualIndex] = actual[lastActualIndex].y != null ? Math.round(actual[lastActualIndex].y) : null;
    for (let j = 0; j < predicted.length; j += 1) {
      predictionSeries[actualCount + j] = predicted[j].y != null ? Math.round(predicted[j].y) : null;
    }
  }

  entry.pendingChart = { labels, actualSeries, predictionSeries };

  if (entry.chart) {
    applyPendingChartUpdate(entry);
  } else if (canvas) {
    const observer = ensureCardChartObserver();
    if (observer) {
      observer.observe(canvas);
    } else {
      ensureChart(entry);
      applyPendingChartUpdate(entry);
    }
  }

  buildPendingSalesView(entry, item);
  if (entry.salesChart) {
    applyPendingSalesChartUpdate(entry);
  } else if (entry.salesChartMiniCanvas && entry.pendingSalesView) {
    const so = ensureCardChartObserver();
    if (so) {
      so.observe(entry.salesChartMiniCanvas);
    } else {
      ensureSalesChart(entry);
      applyPendingSalesChartUpdate(entry);
    }
  }

  const latest = item.latest || {};
  const latestAge = latest.time ? Date.now() - latest.time : Infinity;
  const latestValid = latestAge < THREE_MONTHS_MS;
  const low = latestValid ? getDisplayLowestMirror(latest) : null;
  const high = latestValid ? getDisplayHighestMirror(latest) : null;
  const priceIsLastKnown = latestValid && isShowingLastKnownMirrorPrice(latest);

  if (item.imagePath) {
    setCardImage(img, item.imagePath);
  } else {
    setCardImage(img, null);
  }

  if (item.queryId) {
    artFrame.onclick = () =>
      window.open(`https://www.pathofexile.com/trade/search/Standard/${item.queryId}`, "_blank");
    artFrame.style.cursor = "pointer";
    img.onclick = null;
    img.style.cursor = "inherit";
  } else {
    artFrame.onclick = null;
    artFrame.style.cursor = "default";
    img.onclick = null;
    img.style.cursor = "default";
  }

  const staleSuffix = priceIsLastKnown ? "" : "";
  const priceText =
    low != null && high != null
      ? `Prices: ${formatNumber(low)} to ${formatNumber(high)} mirror${staleSuffix}`
      : low != null
        ? `Price: ${formatNumber(low)} mirror${staleSuffix}`
        : "Price: n/a";
  priceBox.textContent = priceText;

  const spanMs = getChartTimespanMs();
  const agg = aggregateInferenceSignalsOverWindow(item.points, spanMs);
  const soldLine = formatEstimatedSoldLine(agg);
  if (soldLine) {
    salesSummary.textContent = soldLine;
    salesSummary.hidden = false;
  } else {
    salesSummary.textContent = "";
    salesSummary.hidden = true;
  }

  let trendSymbol = "-";
  let trendClass = "flat";
  let trendPercentage = "";
  const valid = sparkValues.filter((v) => v != null && !Number.isNaN(v)).map((v) => Math.round(v));
  if (valid.length >= 2) {
    const first = valid[0];
    const last = valid[valid.length - 1];
    const percentageChange = ((last - first) / first) * 100;
    const roundedPercentage = Math.round(percentageChange);
    if (Math.abs(roundedPercentage) >= 1) {
      trendPercentage = `${roundedPercentage >= 0 ? "+" : ""}${roundedPercentage}% `;
    }
    if (last > first) {
      trendSymbol = "▲";
      trendClass = "up";
    } else if (last < first) {
      trendSymbol = "▼";
      trendClass = "down";
    }
  }

  trend.className = `trend ${trendClass}`;
  trendIndicator.textContent = `${trendPercentage}${trendSymbol}`;

  const listingsCount = latestValid ? latest.totalResults ?? 0 : "n/a";
  trendListings.textContent = `Listings: ${listingsCount}`;

  const canShowListingsPreview = latestValid && Boolean(item.queryId);
  listingsHoverArea.classList.toggle("disabled", !canShowListingsPreview);
  listingsHoverArea.tabIndex = canShowListingsPreview ? 0 : -1;

  if (!canShowListingsPreview) {
    listingsPopoverHeader.textContent = "Listings";
    listingsPopoverSubline.textContent = "";
    listingsPopoverBody.textContent = "Listing details are not available for this item yet.";
    listingsHoverArea.setAttribute("aria-label", "Listing details unavailable");
    const mapEntry = chartMap.get(item.itemName);
    if (mapEntry) {
      // If the user currently has the popover open, don't force-close it just because
      // a refresh cycle temporarily removed/invalidated the queryId (or data went stale).
      // Keep it open and show the "unavailable" message until data returns.
      const isOpen = mapEntry.listingsHoverArea?.classList?.contains("popover-open");
      if (!isOpen) {
        mapEntry.currentQueryId = null;
        mapEntry.loadedQueryId = null;
        mapEntry.loadingQueryId = null;
        stopListingsPopover(mapEntry);
        mapEntry.card.classList.remove("popover-active");
        mapEntry.listingsHoverArea.classList.remove("popover-open");
      }
    }
    return;
  }

  listingsHoverArea.setAttribute("aria-label", "Show listing details");

  const mapEntry = chartMap.get(item.itemName);
  if (!mapEntry) {
    return;
  }

  if (mapEntry.currentQueryId !== item.queryId) {
    mapEntry.currentQueryId = item.queryId;
    mapEntry.loadedQueryId = null;
    mapEntry.loadingQueryId = null;
    listingsPopoverHeader.textContent = "Listings";
    listingsPopoverSubline.textContent = "";
    listingsPopoverBody.textContent = "Hover to load listing details.";

    if (mapEntry.listingsHoverArea.classList.contains("popover-open")) {
      mapEntry.requestListingsPreviewLoad?.();
    }
  }
}

export function updateAllCards(itemsToRender, onFavoriteToggle) {
  dom.cardsEl.querySelector(".empty")?.remove();

  const seen = new Set();
  for (let i = 0; i < itemsToRender.length; i += 1) {
    const item = itemsToRender[i];
    seen.add(item.itemName);
    if (!chartMap.has(item.itemName)) {
      ensureCard(item, onFavoriteToggle);
    }

    const entry = chartMap.get(item.itemName);
    const wasListingsPopoverOpen = Boolean(entry?.listingsHoverArea?.classList?.contains("popover-open"));
    const currentAtIndex = dom.cardsEl.children[i];
    if (currentAtIndex !== entry.card) {
      dom.cardsEl.insertBefore(entry.card, currentAtIndex ?? null);
    }

    // Performance: on browsers without effective `content-visibility` (e.g. Firefox),
    // updating hundreds/thousands of offscreen cards every refresh causes jank.
    // Only update cards near the viewport; apply the latest state when they scroll into view.
    if (entry?.isNearViewport) {
      updateCard(item, onFavoriteToggle);
      entry.pendingItemForViewport = null;
    } else if (entry) {
      entry.pendingItemForViewport = { item, onFavoriteToggle };
    }

    // Keep listings preview open across refreshes if the user had it open.
    // During re-render we may briefly close due to transient payload changes;
    // re-open if the entry is still eligible for listings preview.
    if (wasListingsPopoverOpen) {
      const stillEligible = entry?.listingsHoverArea && !entry.listingsHoverArea.classList.contains("disabled");
      const isOpenNow = Boolean(entry?.listingsHoverArea?.classList?.contains("popover-open"));
      if (stillEligible && !isOpenNow) {
        entry.openPopover?.();
      }
    }
  }

  for (const [key, entry] of chartMap.entries()) {
    if (!seen.has(key)) {
      stopListingsPopover(entry);
      entry.chartTooltipCleanup?.();
      entry.chart?.destroy?.();
      destroySalesChart(entry);
      entry.card.remove();
      chartMap.delete(key);
    }
  }
}

export function showNoFilterResults() {
  dom.cardsEl.innerHTML = '<div class="empty">No items match your filters.</div>';
}
