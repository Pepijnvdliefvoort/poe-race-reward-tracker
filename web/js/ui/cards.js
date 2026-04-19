import { chartMap, dom, MAX_POINTS, THREE_MONTHS_MS, saveFavorites, state } from "../core/state.js";
import { getAvailableLowestPrice } from "../domain/pricing.js";
import { formatNumber, formatTime, getCondensedChartPoints } from "../core/utils.js";
import { stopListingsPopover, wireListingsPopover } from "../cards/listingsPopover.js";

let activeTooltipChart = null;
let tooltipGlobalListenersInstalled = false;

function clearChartTooltip(chart) {
  if (!chart?.tooltip) return;
  chart.tooltip.setActiveElements([], {});
  chart.draw();
}

function clearAllChartTooltips() {
  for (const entry of chartMap.values()) {
    clearChartTooltip(entry?.chart);
  }
  activeTooltipChart = null;
}

function installTooltipGlobalListeners() {
  if (tooltipGlobalListenersInstalled) return;
  tooltipGlobalListenersInstalled = true;

  const clearActive = () => {
    if (!activeTooltipChart) return;
    clearChartTooltip(activeTooltipChart);
    activeTooltipChart = null;
  };

  // If the user interacts anywhere else, the pinned chart tooltip should not remain.
  const clearOnOutsidePress = (event) => {
    if (!activeTooltipChart) return;
    const target = event?.target;
    const canvas = activeTooltipChart?.canvas;
    if (target && canvas && (target === canvas || canvas.contains?.(target))) {
      return;
    }
    clearActive();
  };

  // Clear the pinned tooltip when the gesture ends anywhere (release can happen
  // outside the canvas during scroll/pull-to-refresh).
  if ("PointerEvent" in window) {
    window.addEventListener("pointerdown", clearOnOutsidePress, { passive: true, capture: true });
    window.addEventListener("pointerup", clearActive, { passive: true, capture: true });
    window.addEventListener("pointercancel", clearActive, { passive: true, capture: true });
    // Some browsers can "steal" touch pointers for scrolling without sending a
    // reliable up/cancel to the original target; clearing on scroll is a safe fallback.
  } else {
    window.addEventListener("touchstart", clearOnOutsidePress, { passive: true, capture: true });
    window.addEventListener("touchend", clearActive, { passive: true, capture: true });
    window.addEventListener("touchcancel", clearActive, { passive: true, capture: true });
    // When the browser is actively scrolling, touchmove becomes non-cancelable.
    // This is a strong signal that the user is scrolling/pulling-to-refresh, so
    // we should not keep the chart tooltip pinned.
    window.addEventListener(
      "touchmove",
      (event) => {
        if (!activeTooltipChart) return;
        if (event.cancelable === false) {
          clearActive();
        }
      },
      { passive: true, capture: true }
    );
  }

  // Scrolling should reset *all* pinned value tags/tooltips.
  window.addEventListener("scroll", clearAllChartTooltips, { passive: true });
  window.addEventListener("blur", clearActive, { passive: true });
  window.addEventListener("pagehide", clearActive, { passive: true });
  document.addEventListener(
    "visibilitychange",
    () => {
      if (document.visibilityState !== "visible") clearActive();
    },
    { passive: true }
  );
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
          callbacks: {
            title: () => null,
            label: (ctx) => `${Math.round(ctx.parsed.y)} mirrors`,
          },
          enabled: true,
          mode: "index",
          intersect: false,
          displayColors: false,
          position: "nearest",
          xAlign: "center",
          yAlign: "bottom",
          caretPadding: 6,
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
    plugins: [
      {
        id: "sectionedTooltip",
        afterDatasetsDraw(chartInstance) {
          const datasetMeta = chartInstance.getDatasetMeta(0);
          const dataPoints = datasetMeta.data || [];

          if (dataPoints.length === 0) return;

          const pointPositions = dataPoints.map((point) => ({ x: point.x, y: point.y }));

          chartInstance._sectionTooltipData = {
            pointPositions,
            dataLength: dataPoints.length,
          };
        },
      },
      {
        id: "tooltipActivityTracker",
        afterEvent(chartInstance) {
          const tooltip = chartInstance?.tooltip;
          const active = tooltip?.getActiveElements?.() ?? [];
          if (active.length > 0) {
            activeTooltipChart = chartInstance;
            installTooltipGlobalListeners();
          } else if (activeTooltipChart === chartInstance) {
            activeTooltipChart = null;
          }
        },
      },
    ],
    onHover: (event) => {
      if (!chart._sectionTooltipData || !event.native) {
        clearChartTooltip(chart);
        if (activeTooltipChart === chart) activeTooltipChart = null;
        return;
      }

      const { pointPositions, dataLength } = chart._sectionTooltipData;
      const mouseX = event.native.offsetX;

      if (dataLength === 0) {
        clearChartTooltip(chart);
        if (activeTooltipChart === chart) activeTooltipChart = null;
        return;
      }

      let closestIndex = 0;
      if (dataLength === 1) {
        closestIndex = 0;
      } else {
        for (let i = 0; i < dataLength - 1; i += 1) {
          if (mouseX < pointPositions[i + 1].x) {
            closestIndex = i;
            break;
          }
          closestIndex = i + 1;
        }
      }

      const activePoint = pointPositions[closestIndex];

      chart.tooltip.setActiveElements(
        [{ datasetIndex: 0, index: closestIndex }],
        { x: activePoint.x, y: activePoint.y - 8 }
      );
      chart.draw();
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
    currentQueryId: null,
    loadedQueryId: null,
    loadingQueryId: null,
    listingsRefreshTimer: null,
    handleViewportChange: null,
    openPopover: null,
    closePopover: null,
  };
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
  const cutoff = Date.now() - THREE_MONTHS_MS;
  const rawPoints = (item.points || []).filter((p) => p.time >= cutoff);
  const chartPoints = getCondensedChartPoints(rawPoints, MAX_POINTS);
  const sparkValues = chartPoints.map((p) => p.y);

  card.classList.toggle("next-in-line", item.itemName === state.nextInLineItemName);
  const isFavorited = state.favoriteItems.has(item.itemName);
  card.classList.toggle("favorited", isFavorited);
  favoriteBtn.classList.toggle("checked", isFavorited);
  favoriteBtn.textContent = isFavorited ? "★" : "☆";
  favoriteBtn.setAttribute("aria-label", isFavorited ? `Unfavorite ${item.itemName}` : `Favorite ${item.itemName}`);
  favoriteBtn.title = isFavorited ? "Unfavorite" : "Favorite";

  chart.data.labels = chartPoints.map((p) => formatTime(p.x));
  chart.data.datasets[0].data = chartPoints.map((p) => (p.y != null ? Math.round(p.y) : p.y));
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
      entry.currentQueryId = null;
      entry.loadedQueryId = null;
      entry.loadingQueryId = null;
      stopListingsPopover(entry);
      entry.card.classList.remove("popover-active");
      entry.listingsHoverArea.classList.remove("popover-open");
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
    const currentAtIndex = dom.cardsEl.children[i];
    if (currentAtIndex !== entry.card) {
      dom.cardsEl.insertBefore(entry.card, currentAtIndex ?? null);
    }

    updateCard(item, onFavoriteToggle);
  }

  for (const [key, entry] of chartMap.entries()) {
    if (!seen.has(key)) {
      stopListingsPopover(entry);
      entry.chart.destroy();
      entry.card.remove();
      chartMap.delete(key);
    }
  }
}

export function showNoFilterResults() {
  dom.cardsEl.innerHTML = '<div class="empty">No items match your filters.</div>';
}
