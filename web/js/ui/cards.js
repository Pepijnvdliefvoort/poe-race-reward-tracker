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
import { getAvailableLowestPrice } from "../domain/pricing.js";
import { formatNumber, formatTime, getChartSeriesWithPrediction } from "../core/utils.js";
import { stopListingsPopover, wireListingsPopover } from "../cards/listingsPopover.js";

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

  artFrame.append(img);

  const priceBox = document.createElement("div");
  priceBox.className = "price-box";

  const chartWrap = document.createElement("div");
  chartWrap.className = "chart-wrap";
  const canvas = document.createElement("canvas");
  chartWrap.appendChild(canvas);

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
  card.append(title, artFrame, priceBox, chartWrap, trend);
  dom.cardsEl.appendChild(card);

  ensureTooltipOffsetPositioner();

  const chart = new Chart(canvas.getContext("2d"), {
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
          padding: 0,
          cornerRadius: 0,
          backgroundColor: "rgba(0, 0, 0, 0)",
          borderColor: "rgba(0, 0, 0, 0)",
          borderWidth: 0,
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

  entry = {
    card,
    favoriteBtn,
    img,
    artFrame,
    priceBox,
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
    chart,
    chartTooltipCleanup: null,
    currentQueryId: null,
    loadedQueryId: null,
    loadingQueryId: null,
    listingsRefreshTimer: null,
    handleViewportChange: null,
    openPopover: null,
    closePopover: null,
  };
  entry.chartTooltipCleanup = wireMobileTooltipDismiss(chart);
  chartMap.set(key, entry);
  wireListingsPopover(entry);
  return entry;
}

export function updateCard(item, onFavoriteToggle) {
  const {
    card,
    favoriteBtn,
    img,
    artFrame,
    priceBox,
    trend,
    trendIndicator,
    trendListings,
    listingsHoverArea,

    listingsPopoverHeader,
    listingsPopoverBody,
    listingsPopoverSubline,
    chart,
  } = ensureCard(item, onFavoriteToggle);
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

  chart.data.labels = chartPoints.map((p) => formatTime(p.x));

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

  chart.data.datasets[0].data = actualSeries;
  chart.data.datasets[1].data = predictionSeries;
  chart.update();

  const latest = item.latest || {};
  const latestAge = latest.time ? Date.now() - latest.time : Infinity;
  const latestValid = latestAge < THREE_MONTHS_MS;
  const low = latestValid ? latest.lowestMirror : null;
  const high = latestValid ? latest.highestMirror : null;

  if (item.imagePath) {
    img.src = item.imagePath;
    img.style.display = "block";
  } else {
    img.style.display = "none";
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

  const priceText =
    low != null && high != null
      ? `Prices: ${formatNumber(low)} to ${formatNumber(high)} mirror`
      : low != null
        ? `Price: ${formatNumber(low)} mirror`
        : "Price: n/a";
  priceBox.textContent = priceText;

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
    const entry = chartMap.get(item.itemName);
    if (entry) {
      // If the user currently has the popover open, don't force-close it just because
      // a refresh cycle temporarily removed/invalidated the queryId (or data went stale).
      // Keep it open and show the "unavailable" message until data returns.
      const isOpen = entry.listingsHoverArea?.classList?.contains("popover-open");
      if (!isOpen) {
        entry.currentQueryId = null;
        entry.loadedQueryId = null;
        entry.loadingQueryId = null;
        stopListingsPopover(entry);
        entry.card.classList.remove("popover-active");
        entry.listingsHoverArea.classList.remove("popover-open");
      }
    }
    return;
  }

  listingsHoverArea.setAttribute("aria-label", "Show listing details");

  const entry = chartMap.get(item.itemName);
  if (!entry) {
    return;
  }

  if (entry.currentQueryId !== item.queryId) {
    entry.currentQueryId = item.queryId;
    entry.loadedQueryId = null;
    entry.loadingQueryId = null;
    listingsPopoverHeader.textContent = "Listings";
    listingsPopoverSubline.textContent = "";
    listingsPopoverBody.textContent = "Hover to load listing details.";

    if (entry.listingsHoverArea.classList.contains("popover-open")) {
      entry.requestListingsPreviewLoad?.();
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

    updateCard(item, onFavoriteToggle);

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
      entry.chart.destroy();
      entry.card.remove();
      chartMap.delete(key);
    }
  }
}

export function showNoFilterResults() {
  dom.cardsEl.innerHTML = '<div class="empty">No items match your filters.</div>';
}
