import { chartMap, dom, MAX_POINTS, THREE_MONTHS_MS, saveFavorites, state } from "./state.js";
import { formatNumber, formatTime, getCondensedChartPoints } from "./utils.js";

export function getTrendValue(item) {
  const cutoff = Date.now() - THREE_MONTHS_MS;
  const rawPoints = (item.points || []).filter((p) => p.time >= cutoff);
  const chartPoints = getCondensedChartPoints(rawPoints, MAX_POINTS);
  const valid = chartPoints.map((p) => p.y).filter((v) => v != null && !Number.isNaN(v));

  if (valid.length >= 2) {
    const first = valid[0];
    const last = valid[valid.length - 1];
    return last - first;
  }
  return 0;
}

export function getTrendPercentage(item) {
  const cutoff = Date.now() - THREE_MONTHS_MS;
  const rawPoints = (item.points || []).filter((p) => p.time >= cutoff);
  const chartPoints = getCondensedChartPoints(rawPoints, MAX_POINTS);
  const valid = chartPoints.map((p) => p.y).filter((v) => v != null && !Number.isNaN(v));

  if (valid.length >= 2) {
    const first = valid[0];
    const last = valid[valid.length - 1];
    if (first === 0) return 0;
    return ((last - first) / first) * 100;
  }
  return 0;
}

export function getTrendDirection(item) {
  const percentage = getTrendPercentage(item);
  const rounded = Math.round(percentage);
  if (rounded > 0) return "up";
  if (rounded < 0) return "down";
  return "flat";
}

export function getAvailableLowestPrice(item) {
  const latest = item.latest;
  if (!latest?.time || Date.now() - latest.time >= THREE_MONTHS_MS) {
    return null;
  }

  const low = latest.lowestMirror;
  return low == null || Number.isNaN(low) ? null : low;
}

function compareByPriceWithMissingLast(a, b, direction) {
  const aPrice = getAvailableLowestPrice(a);
  const bPrice = getAvailableLowestPrice(b);
  const aMissing = aPrice == null;
  const bMissing = bPrice == null;

  if (aMissing && bMissing) {
    return 0;
  }
  if (aMissing) {
    return 1;
  }
  if (bMissing) {
    return -1;
  }

  return direction === "asc" ? aPrice - bPrice : bPrice - aPrice;
}

export function applySorting(filtered, filters) {
  if (filters.priceSort === "asc") {
    filtered.sort((a, b) => compareByPriceWithMissingLast(a, b, "asc"));
  } else if (filters.priceSort === "desc") {
    filtered.sort((a, b) => compareByPriceWithMissingLast(a, b, "desc"));
  }

  if (filters.trendSort === "highest") {
    filtered.sort((a, b) => compareTrendHighest(a, b));
  } else if (filters.trendSort === "lowest") {
    filtered.sort((a, b) => compareTrendLowest(a, b));
  }

  return filtered;
}

function compareTrendHighest(a, b) {
  // Direction priority: up > flat > down
  const directionOrder = { up: 0, flat: 1, down: 2 };
  const dirA = getTrendDirection(a);
  const dirB = getTrendDirection(b);

  if (directionOrder[dirA] !== directionOrder[dirB]) {
    return directionOrder[dirA] - directionOrder[dirB];
  }

  // Within same direction, sort by percentage
  return getTrendPercentage(b) - getTrendPercentage(a);
}

function compareTrendLowest(a, b) {
  // Direction priority: down > flat > up
  const directionOrder = { down: 0, flat: 1, up: 2 };
  const dirA = getTrendDirection(a);
  const dirB = getTrendDirection(b);

  if (directionOrder[dirA] !== directionOrder[dirB]) {
    return directionOrder[dirA] - directionOrder[dirB];
  }

  // Within same direction, sort by percentage
  return getTrendPercentage(a) - getTrendPercentage(b);
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
  const trendListings = document.createElement("span");
  trendListings.className = "trend-listings";
  trend.append(trendLabel, trendIndicator, trendListings);

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
    ],
    onHover: (event) => {
      if (!chart._sectionTooltipData || !event.native) {
        chart.tooltip.setActiveElements([], {});
        chart.draw();
        return;
      }

      const { pointPositions, dataLength } = chart._sectionTooltipData;
      const mouseX = event.native.offsetX;

      if (dataLength === 0) {
        chart.tooltip.setActiveElements([], {});
        chart.draw();
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

  entry = { card, favoriteBtn, img, artFrame, priceBox, trend, trendIndicator, trendListings, chart };
  chartMap.set(key, entry);
  return entry;
}

export function updateCard(item, onFavoriteToggle) {
  const { card, favoriteBtn, img, artFrame, priceBox, trend, trendIndicator, trendListings, chart } = ensureCard(item, onFavoriteToggle);
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
  trendListings.textContent = `   Listings: ${latestValid ? (latest.totalResults ?? 0) : "n/a"}`;
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
      entry.chart.destroy();
      entry.card.remove();
      chartMap.delete(key);
    }
  }
}

export function showNoFilterResults() {
  dom.cardsEl.innerHTML = '<div class="empty">No items match your filters.</div>';
}
