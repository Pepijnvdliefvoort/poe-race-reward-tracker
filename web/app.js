const REFRESH_MS = 4000;
const MAX_POINTS = 14;
const ONE_MONTH_MS = 30 * 24 * 60 * 60 * 1000;

const cardsEl = document.getElementById("cards");
const overviewEl = document.getElementById("overview");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const searchInput = document.getElementById("searchInput");
const priceSortSelect = document.getElementById("priceSort");
const trendSortSelect = document.getElementById("trendSort");

const chartMap = new Map();

let filters = {
  search: "",
  priceSort: "",
  trendSort: "",
};

let currentItems = [];
let nextInLineItemName = null;

// Setup filter event listeners
searchInput.addEventListener("input", (e) => {
  filters.search = e.target.value.toLowerCase();
  applyFiltersAndSort();
});

priceSortSelect.addEventListener("change", (e) => {
  filters.priceSort = e.target.value;
  applyFiltersAndSort();
});

trendSortSelect.addEventListener("change", (e) => {
  filters.trendSort = e.target.value;
  applyFiltersAndSort();
});

function getTrendValue(item) {
  const points = (item.points || []).slice(-MAX_POINTS);
  const sparkValues = points.map((p) => p.medianMirror ?? p.lowestMirror ?? p.highestMirror);
  const valid = sparkValues.filter((v) => v != null && !Number.isNaN(v));
  
  if (valid.length >= 2) {
    const prev = valid[valid.length - 2];
    const curr = valid[valid.length - 1];
    return curr - prev;
  }
  return 0;
}

function getAvailableLowestPrice(item) {
  const latest = item.latest;
  if (!latest?.time || Date.now() - latest.time >= ONE_MONTH_MS) {
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

function getNextInLineItemName(items) {
  if (!items.length) {
    return null;
  }

  let latestIndex = -1;
  let latestCycle = -Infinity;
  let latestTime = -Infinity;

  for (let i = 0; i < items.length; i += 1) {
    const latest = items[i].latest;
    if (!latest) {
      continue;
    }

    const cycle = latest.cycle ?? -Infinity;
    const time = latest.time ?? -Infinity;

    if (
      cycle > latestCycle ||
      (cycle === latestCycle && time > latestTime)
    ) {
      latestCycle = cycle;
      latestTime = time;
      latestIndex = i;
    }
  }

  if (latestIndex === -1) {
    return null;
  }

  const nextIndex = (latestIndex + 1) % items.length;
  return items[nextIndex]?.itemName ?? null;
}

function updateAllCards() {
  // Update all existing cards without re-adding to DOM (prevents animation)
  const seen = new Set();
  for (const item of currentItems) {
    seen.add(item.itemName);
    if (chartMap.has(item.itemName)) {
      updateCard(item);
    } else {
      ensureCard(item);
      updateCard(item);
    }
  }

  // Remove cards not in current items
  for (const [key, entry] of chartMap.entries()) {
    if (!seen.has(key)) {
      entry.chart.destroy();
      entry.card.remove();
      chartMap.delete(key);
    }
  }
}

function applyFiltersAndSort() {
  let filtered = [...currentItems];

  // Apply search filter
  if (filters.search) {
    filtered = filtered.filter((item) =>
      item.itemName.toLowerCase().includes(filters.search)
    );
  }

  // Apply sorting
  if (filters.priceSort === "asc") {
    filtered.sort((a, b) => compareByPriceWithMissingLast(a, b, "asc"));
  } else if (filters.priceSort === "desc") {
    filtered.sort((a, b) => compareByPriceWithMissingLast(a, b, "desc"));
  }

  if (filters.trendSort === "highest") {
    filtered.sort((a, b) => getTrendValue(b) - getTrendValue(a));
  } else if (filters.trendSort === "lowest") {
    filtered.sort((a, b) => getTrendValue(a) - getTrendValue(b));
  }

  // Reorder cards based on filtered list
  const seen = new Set();
  for (let i = 0; i < filtered.length; i += 1) {
    const item = filtered[i];
    seen.add(item.itemName);
    ensureCard(item);
    const entry = chartMap.get(item.itemName);
    const currentAtIndex = cardsEl.children[i];
    if (currentAtIndex !== entry.card) {
      cardsEl.insertBefore(entry.card, currentAtIndex ?? null);
    }
    updateCard(item);
  }

  // Remove cards not in filtered list
  for (const [key, entry] of chartMap.entries()) {
    if (!seen.has(key)) {
      entry.chart.destroy();
      entry.card.remove();
      chartMap.delete(key);
    }
  }

  if (filtered.length === 0) {
    cardsEl.innerHTML = '<div class="empty">No items match your filters.</div>';
  }
}

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "n/a";
  }
  if (Math.abs(value) >= 1000) {
    return value.toFixed(0);
  }
  return value.toFixed(2).replace(/\.00$/, "");
}

function formatTime(ms, withSeconds = false) {
  const d = new Date(ms);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined,
  });
}

function setStatus(state, text) {
  statusDot.classList.remove("ok", "warn", "error");
  statusDot.classList.add(state);
  statusText.textContent = text;
}

function statTile(label, value) {
  const tile = document.createElement("article");
  tile.className = "stat-tile";
  tile.innerHTML = `
    <div class="stat-label">${label}</div>
    <div class="stat-value">${value}</div>
  `;
  return tile;
}

function updateOverview(payload) {
  const items = payload.items || [];
  const totalRows = payload.rowCount || 0;
  const nextPollTime = payload.nextPollTime;

  const tiles = [
    statTile("Tracked Items", String(items.length)),
    statTile("Data points", String(totalRows)),
  ];

  if (nextPollTime) {
    const nextPollDate = new Date(nextPollTime);
    const timeString = nextPollDate.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
    tiles.push(statTile("Next Poll", timeString));
  }

  overviewEl.replaceChildren(...tiles);
}

function ensureCard(item) {
  const key = item.itemName;
  let entry = chartMap.get(key);

  if (entry) {
    return entry;
  }

  const card = document.createElement("article");
  card.className = "card card-enter";
  card.addEventListener("animationend", () => {
    card.classList.remove("card-enter");
  }, { once: true });

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

  card.append(title, artFrame, priceBox, chartWrap, trend);
  cardsEl.appendChild(card);

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
            label: (ctx) => `Price ${formatNumber(ctx.parsed.y)}m`,
          },
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

  entry = { card, img, artFrame, priceBox, trend, chart };
  chartMap.set(key, entry);
  return entry;
}

function updateCard(item) {
  const { card, img, artFrame, priceBox, trend, chart } = ensureCard(item);
  const cutoff = Date.now() - ONE_MONTH_MS;
  const points = (item.points || []).filter((p) => p.time >= cutoff).slice(-MAX_POINTS);

  card.classList.toggle("next-in-line", item.itemName === nextInLineItemName);

  const sparkValues = points.map((p) => p.medianMirror ?? p.lowestMirror ?? p.highestMirror);
  chart.data.labels = points.map((p) => formatTime(p.time));
  chart.data.datasets[0].data = sparkValues;
  chart.update();

  const latest = item.latest || {};
  const latestAge = latest.time ? Date.now() - latest.time : Infinity;
  const latestValid = latestAge < ONE_MONTH_MS;
  const low = latestValid ? latest.lowestMirror : null;
  const high = latestValid ? latest.highestMirror : null;

  if (item.imagePath) {
    img.src = item.imagePath;
    img.style.display = "block";
  } else {
    img.style.display = "none";
  }

  if (item.queryId) {
    artFrame.onclick = () => window.open(`https://www.pathofexile.com/trade/search/Standard/${item.queryId}`, "_blank");
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
  const valid = sparkValues.filter((v) => v != null && !Number.isNaN(v));
  if (valid.length >= 2) {
    const prev = valid[valid.length - 2];
    const curr = valid[valid.length - 1];
    if (curr > prev) {
      trendSymbol = "▲";
      trendClass = "up";
    } else if (curr < prev) {
      trendSymbol = "▼";
      trendClass = "down";
    }
  }

  trend.className = `trend ${trendClass}`;
  trend.textContent = `Price Trend: ${trendSymbol}   Listings: ${latestValid ? (latest.totalResults ?? 0) : "n/a"}`;
}

function render(payload) {
  updateOverview(payload);
  currentItems = payload.items || [];
  nextInLineItemName = getNextInLineItemName(currentItems);

  if (!currentItems.length) {
    cardsEl.innerHTML = '<div class="empty">No item rows yet. Start your poller and wait for CSV updates.</div>';
    return;
  }

  // If any filters are active, apply them. Otherwise just update card data
  const hasActiveFilters = filters.search || filters.priceSort || filters.trendSort;
  if (hasActiveFilters) {
    applyFiltersAndSort();
  } else {
    updateAllCards();
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/prices", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const payload = await response.json();
    render(payload);
    const latestTs = (payload.items || [])
      .flatMap((item) => item.points || [])
      .map((point) => point.time)
      .filter(Boolean)
      .reduce((max, t) => Math.max(max, t), 0);
    const timeLabel = latestTs ? formatTime(latestTs, true) : formatTime(Date.now(), true);
    const ageMs = latestTs ? Date.now() - latestTs : 0;
    const stale = latestTs && ageMs > 60 * 30 * 1000;
    setStatus(stale ? "warn" : "ok", stale ? `Stale ${timeLabel}` : `Live ${timeLabel}`);
  } catch (error) {
    setStatus("error", `Error: ${error.message}`);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
