const REFRESH_MS = 30_000;
const MAX_POINTS = 12;

const els = {
  statusDot: () => document.getElementById("aaLadderStatusDot"),
  statusText: () => document.getElementById("aaLadderStatusText"),
  inlineStatus: () => document.getElementById("aaLadderInlineStatus"),
  itemInput: () => document.getElementById("aaLadderItemInput"),
  itemDatalist: () => document.getElementById("aaLadderItemDatalist"),
  addBtn: () => document.getElementById("aaLadderAddBtn"),
  clearBtn: () => document.getElementById("aaLadderClearBtn"),
  list: () => document.getElementById("aaLadderList"),
};

/** @type {Array<{id:number, query:string, status:'loading'|'ok'|'error', variant:any, error:string}>} */
let cards = [];
let nextCardId = 1;

function setTopStatus(text, ok = true) {
  const dot = els.statusDot();
  const label = els.statusText();
  if (label) label.textContent = text;
  if (dot) {
    dot.classList.remove("ok", "warn", "err");
    dot.classList.add(ok ? "ok" : "warn");
  }
}

function setInlineStatus(text, isError = false) {
  const el = els.inlineStatus();
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("aa-ladder-inline-status--error", Boolean(isError && text));
}

function requestUrl(item) {
  const params = new URLSearchParams();
  params.set("maxPricePoints", String(MAX_POINTS));
  params.set("item", item);
  return `/api/market/aa-price-points?${params.toString()}`;
}

function normalizeName(value) {
  return String(value || "").trim().toLowerCase();
}

function pickBestVariant(query, variants) {
  const q = normalizeName(query);
  if (!Array.isArray(variants) || !variants.length) return null;
  const exact = variants.find((v) => {
    const displayName = normalizeName(v?.displayName);
    const itemName = normalizeName(v?.itemName);
    return displayName === q || itemName === q;
  });
  if (exact) return exact;
  const prefix = variants.find((v) => normalizeName(v?.displayName || v?.itemName).startsWith(q));
  if (prefix) return prefix;
  return variants[0];
}

async function loadItemSuggestions() {
  const list = els.itemDatalist();
  if (!list) return;

  try {
    const params = new URLSearchParams();
    params.set("mode", "aa");
    params.set("maxPricePoints", "1");
    const res = await fetch(`/api/market/aa-price-points?${params.toString()}`, { cache: "no-store" });
    if (!res.ok) return;
    const payload = await res.json();
    const variants = Array.isArray(payload?.variants) ? payload.variants : [];

    const unique = [];
    const seen = new Set();
    for (const v of variants) {
      const name = String(v?.displayName || v?.itemName || "").trim();
      if (!name) continue;
      const key = name.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      unique.push(name);
    }

    unique.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    list.innerHTML = "";
    for (const name of unique) {
      const opt = document.createElement("option");
      opt.value = name;
      list.appendChild(opt);
    }
  } catch {
    // Ignore suggestion load failures; filtering still works with manual input.
  }
}

function renderPlaceholder() {
  const root = els.list();
  if (!root) return;
  root.innerHTML = "";

  const placeholder = document.createElement("article");
  placeholder.className = "aa-ladder-placeholder";

  const title = document.createElement("h3");
  title.className = "aa-ladder-placeholder-title";
  title.textContent = "Add AA cards";

  const body = document.createElement("p");
  body.className = "aa-ladder-placeholder-body";
  body.textContent = "Type an AA name and press Enter (or Add card). Cards fill left-to-right and wrap to new rows automatically.";

  placeholder.append(title, body);
  root.appendChild(placeholder);
}

function removeCard(cardId) {
  cards = cards.filter((c) => c.id !== cardId);
  renderBoard();
}

function renderBoard() {
  const root = els.list();
  if (!root) return;
  root.innerHTML = "";

  if (!cards.length) {
    renderPlaceholder();
    return;
  }

  for (const card of cards) {
    const article = document.createElement("article");
    article.className = "aa-ladder-card";

    const head = document.createElement("div");
    head.className = "aa-ladder-card-head";

    const title = document.createElement("h3");
    title.className = "aa-ladder-card-title";
    title.textContent = card.query;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "aa-ladder-card-remove";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => removeCard(card.id));

    head.append(title, removeBtn);
    article.appendChild(head);

    if (card.status === "loading") {
      const loading = document.createElement("p");
      loading.className = "aa-ladder-card-hint";
      loading.textContent = "Loading…";
      article.appendChild(loading);
      root.appendChild(article);
      continue;
    }

    if (card.status === "error") {
      const err = document.createElement("p");
      err.className = "aa-ladder-card-hint aa-ladder-card-hint--error";
      err.textContent = card.error || "Failed to load.";
      article.appendChild(err);
      root.appendChild(article);
      continue;
    }

    const variant = card.variant;
    const total = document.createElement("p");
    total.className = "aa-ladder-card-total";
    total.textContent = `${Number(variant?.totalListings || 0)} listings`;
    article.appendChild(total);

    const points = document.createElement("ul");
    points.className = "aa-ladder-points";

    const rows = Array.isArray(variant?.pricePoints) ? variant.pricePoints : [];
    for (const p of rows) {
      const li = document.createElement("li");
      li.className = "aa-ladder-point";
      li.textContent = `${Number(p?.listingCount || 0)}x at ${String(p?.label || "?")}`;
      points.appendChild(li);
    }
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "aa-ladder-point aa-ladder-point--empty";
      li.textContent = "No price points";
      points.appendChild(li);
    }

    article.appendChild(points);
    root.appendChild(article);
  }
}

async function refreshCard(card) {
  try {
    const res = await fetch(requestUrl(card.query), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const payload = await res.json();
    const variants = Array.isArray(payload?.variants) ? payload.variants : [];
    const picked = pickBestVariant(card.query, variants);
    if (!picked) {
      card.status = "error";
      card.error = "No matches found.";
      card.variant = null;
      return;
    }

    card.status = "ok";
    card.error = "";
    card.variant = {
      displayName: String(picked?.displayName || picked?.itemName || card.query),
      totalListings: Number(picked?.totalListings || 0),
      pricePoints: Array.isArray(picked?.pricePoints) ? picked.pricePoints : [],
    };
  } catch {
    card.status = "error";
    card.error = "Failed to load ladder data.";
    card.variant = null;
  }
}

async function addCardFromInput() {
  const input = els.itemInput();
  const query = String(input?.value || "").trim();
  if (!query) {
    setInlineStatus("Enter an AA name first.", true);
    return;
  }

  const key = normalizeName(query);
  const exists = cards.some((c) => normalizeName(c.query) === key);
  if (exists) {
    setInlineStatus(`\"${query}\" is already added.`, false);
    return;
  }

  const card = {
    id: nextCardId++,
    query,
    status: "loading",
    variant: null,
    error: "",
  };
  cards.push(card);
  setTopStatus("Loading card…", true);
  setInlineStatus(`Added \"${query}\".`, false);
  renderBoard();
  if (input) input.value = "";

  await refreshCard(card);
  const okCount = cards.filter((c) => c.status === "ok").length;
  setTopStatus("Cards updated", true);
  setInlineStatus(`${okCount}/${cards.length} cards loaded.`, false);
  renderBoard();
}

function clearCards() {
  cards = [];
  setTopStatus("Ready", true);
  setInlineStatus("Enter an AA name and add a card.", false);
  renderPlaceholder();
}

async function refreshAllCards() {
  if (!cards.length) return;
  await Promise.all(cards.map((card) => refreshCard(card)));
  const okCount = cards.filter((c) => c.status === "ok").length;
  setTopStatus("Cards updated", true);
  setInlineStatus(`${okCount}/${cards.length} cards loaded.`, false);
  renderBoard();
}

export function initAaLadder() {
  const input = els.itemInput();
  const addBtn = els.addBtn();
  const clearBtn = els.clearBtn();

  if (addBtn) {
    addBtn.addEventListener("click", () => {
      addCardFromInput();
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      clearCards();
    });
  }
  if (input) {
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addCardFromInput();
    });
  }

  renderPlaceholder();
  setInlineStatus("Enter an AA name and add a card.", false);
  loadItemSuggestions();

  setInterval(async () => {
    await refreshAllCards();
  }, REFRESH_MS);
}
