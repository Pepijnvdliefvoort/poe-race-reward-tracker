import { buildPricesApiUrl } from "../core/state.js";
import { formatMirror } from "../core/utils.js";

const STORAGE_KEY = "pmf.altArtsHoldings.v1";
const PRICES_REFRESH_MS = 30_000;

/** @type {Map<string, { variantKey: string, itemName: string, baseItemName: string, mode: string, imageNameFilter: string | null, imagePath: string | null, lowestMirror: number | null }>} */
let catalogByKey = new Map();
/** @type {Map<string, string>} lowercase name -> baseItemName */
let baseNameLookup = new Map();
/** @type {string[]} */
let baseItemNames = [];
let sellExpandedId = null;
let catalogLoaded = false;
let selectedVariantKey = null;

const els = {
  summary: () => document.getElementById("altArtsSummary"),
  addForm: () => document.getElementById("altArtsAddForm"),
  itemInput: () => document.getElementById("altArtsItemInput"),
  purchaseInput: () => document.getElementById("altArtsPurchaseInput"),
  dateInput: () => document.getElementById("altArtsDateInput"),
  notesInput: () => document.getElementById("altArtsNotesInput"),
  formHint: () => document.getElementById("altArtsFormHint"),
  datalist: () => document.getElementById("altArtsItemDatalist"),
  activeTbody: () => document.getElementById("altArtsActiveTbody"),
  activeEmpty: () => document.getElementById("altArtsActiveEmpty"),
  soldTbody: () => document.getElementById("altArtsSoldTbody"),
  soldEmpty: () => document.getElementById("altArtsSoldEmpty"),
  soldCount: () => document.getElementById("altArtsSoldCount"),
  statusDot: () => document.getElementById("altArtsStatusDot"),
  statusText: () => document.getElementById("altArtsStatusText"),
  addBtn: () => document.getElementById("altArtsAddBtn"),
  variantWrap: () => document.getElementById("altArtsVariantWrap"),
  variantPicker: () => document.getElementById("altArtsVariantPicker"),
};

function newId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `lot-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function readState() {
  const empty = () => ({ active: [], sold: [] });
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return empty();
    const parsed = JSON.parse(raw);
    const active = Array.isArray(parsed?.active)
      ? parsed.active
          .map(normalizeActiveLot)
          .filter(Boolean)
      : [];
    const sold = Array.isArray(parsed?.sold)
      ? parsed.sold
          .map(normalizeSoldLot)
          .filter(Boolean)
      : [];
    return { active, sold };
  } catch {
    return empty();
  }
}

function variantKeyFromParts(itemName, imageNameFilter) {
  const name = String(itemName || "").trim();
  const filter = imageNameFilter ? String(imageNameFilter).trim() : "";
  return filter ? `${name}::${filter}` : name;
}

function variantLabel(variant) {
  if (!variant) return "";
  const mode = String(variant.mode || "").trim().toLowerCase();
  if (mode === "normal") return "Normal";
  if (variant.imageNameFilter) {
    const stem = String(variant.imageNameFilter).replace(/\.png$/i, "");
    return `Alternate art (${stem})`;
  }
  return "Alternate art";
}

function normalizeActiveLot(row) {
  if (!row || typeof row !== "object") return null;
  const itemName = String(row.itemName || "").trim();
  const purchaseMirror = Number(row.purchaseMirror);
  if (!itemName || !Number.isFinite(purchaseMirror) || purchaseMirror < 0) return null;
  let variantKey = String(row.variantKey || "").trim();
  if (!variantKey) {
    variantKey = resolveVariantKeyFromItemName(itemName) || itemName;
  }
  const catalog = catalogByKey.get(variantKey);
  return {
    id: String(row.id || newId()),
    itemName: catalog?.itemName || itemName,
    variantKey,
    baseItemName: catalog?.baseItemName || String(row.baseItemName || itemName).trim(),
    purchaseMirror,
    purchasedAt: row.purchasedAt ? String(row.purchasedAt) : new Date().toISOString(),
    notes: row.notes != null ? String(row.notes) : "",
  };
}

function normalizeSoldLot(row) {
  if (!row || typeof row !== "object") return null;
  const itemName = String(row.itemName || "").trim();
  const purchaseMirror = Number(row.purchaseMirror);
  const saleMirror = Number(row.saleMirror);
  const profitMirror = Number(row.profitMirror);
  if (!itemName || !Number.isFinite(purchaseMirror) || !Number.isFinite(saleMirror)) return null;
  let variantKey = String(row.variantKey || "").trim();
  if (!variantKey) {
    variantKey = resolveVariantKeyFromItemName(itemName) || itemName;
  }
  const catalog = catalogByKey.get(variantKey);
  return {
    id: String(row.id || newId()),
    itemName: catalog?.itemName || itemName,
    variantKey,
    baseItemName: catalog?.baseItemName || String(row.baseItemName || itemName).trim(),
    purchaseMirror,
    saleMirror,
    profitMirror: Number.isFinite(profitMirror)
      ? profitMirror
      : saleMirror - purchaseMirror,
    purchasedAt: row.purchasedAt ? String(row.purchasedAt) : "",
    soldAt: row.soldAt ? String(row.soldAt) : new Date().toISOString(),
    notes: row.notes != null ? String(row.notes) : "",
  };
}

function saveState(state) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // ignore
  }
}

function formatMirrorDisplay(v) {
  if (v == null) return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return "";
  return formatMirror(n);
}

function buildMirrorPriceNode(v) {
  const wrap = document.createElement("span");
  wrap.className = "cmp-price-wrap";

  const amountEl = document.createElement("span");
  amountEl.className = "cmp-price-amount";
  amountEl.textContent = formatMirrorDisplay(v);

  const icon = document.createElement("img");
  icon.className = "cmp-price-icon";
  icon.src = "/assets/MirrorofKalandra.png";
  icon.alt = "Mirror of Kalandra";
  icon.decoding = "async";
  icon.loading = "lazy";
  icon.width = 16;
  icon.height = 16;

  wrap.append(amountEl, icon);
  return wrap;
}

function deltaPillClass(delta) {
  if (delta == null) return "cmp-missing";
  const d = Number(delta);
  if (!Number.isFinite(d)) return "cmp-missing";
  if (Math.abs(d) < 1e-9) return "cmp-eq";
  return d > 0 ? "cmp-ok" : "cmp-bad";
}

function formatDeltaMirror(delta) {
  const d = Number(delta);
  if (!Number.isFinite(d)) return "—";
  const sign = d > 0 ? "+" : "";
  return `${sign}${formatMirrorDisplay(d)}`;
}

function formatShortDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString();
}

function parsePurchaseDateInput(value) {
  const s = String(value || "").trim();
  if (!s) return new Date().toISOString();
  const d = new Date(`${s}T12:00:00`);
  if (Number.isNaN(d.getTime())) return new Date().toISOString();
  return d.toISOString();
}

function setStatus(text, ok = true) {
  const dot = els.statusDot();
  const label = els.statusText();
  if (label) label.textContent = text;
  if (dot) {
    dot.classList.remove("ok", "warn", "err");
    dot.classList.add(ok ? "ok" : "warn");
  }
}

function setFormHint(text, isError = false) {
  const el = els.formHint();
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("alt-arts-form-hint--error", Boolean(isError && text));
}

function normalizeLookupKey(name) {
  return String(name || "").trim().toLowerCase();
}

function resolveVariantKeyFromItemName(itemName) {
  const trimmed = String(itemName || "").trim();
  if (!trimmed) return null;
  if (catalogByKey.has(trimmed)) return trimmed;
  for (const v of catalogByKey.values()) {
    if (v.itemName === trimmed) return v.variantKey;
  }
  return null;
}

function resolveBaseItemName(raw) {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return null;
  const viaLookup = baseNameLookup.get(normalizeLookupKey(trimmed));
  if (viaLookup) return viaLookup;
  const key = resolveVariantKeyFromItemName(trimmed);
  if (key && catalogByKey.has(key)) {
    return catalogByKey.get(key).baseItemName;
  }
  return null;
}

function getVariantsForBase(baseName) {
  const base = String(baseName || "").trim();
  if (!base) return [];
  return [...catalogByKey.values()]
    .filter((v) => v.baseItemName === base)
    .sort((a, b) => variantLabel(a).localeCompare(variantLabel(b), undefined, { sensitivity: "base" }));
}

function getVariantForLot(lot) {
  const key = lot?.variantKey || resolveVariantKeyFromItemName(lot?.itemName);
  if (key && catalogByKey.has(key)) return catalogByKey.get(key);
  return null;
}

function getSelectedVariant() {
  if (selectedVariantKey && catalogByKey.has(selectedVariantKey)) {
    return catalogByKey.get(selectedVariantKey);
  }
  const raw = String(els.itemInput()?.value || "").trim();
  const base = resolveBaseItemName(raw);
  const variants = base ? getVariantsForBase(base) : [];
  if (variants.length === 1) return variants[0];
  return null;
}

function syncSelectedVariantForInput() {
  const raw = String(els.itemInput()?.value || "").trim();
  const base = resolveBaseItemName(raw);
  const variants = base ? getVariantsForBase(base) : [];
  if (!variants.length) {
    selectedVariantKey = null;
    return;
  }

  const rawKey = normalizeLookupKey(raw);
  const exact = variants.find((v) => normalizeLookupKey(v.itemName) === rawKey);
  if (exact) {
    selectedVariantKey = exact.variantKey;
    return;
  }

  if (selectedVariantKey && variants.some((v) => v.variantKey === selectedVariantKey)) {
    return;
  }
  selectedVariantKey = variants.length === 1 ? variants[0].variantKey : null;
}

function appendVariantTileMedia(tile, variant) {
  if (variant.imagePath) {
    const img = document.createElement("img");
    img.className = "alt-arts-variant-tile-img";
    img.src = variant.imagePath;
    img.alt = "";
    img.decoding = "async";
    img.loading = "lazy";
    tile.appendChild(img);
    return;
  }
  const placeholder = document.createElement("span");
  placeholder.className = "alt-arts-variant-tile-placeholder";
  placeholder.textContent = "?";
  placeholder.setAttribute("aria-hidden", "true");
  tile.appendChild(placeholder);
}

function buildVariantTile(variant, { selectable, selected }) {
  const tile = document.createElement(selectable ? "button" : "div");
  const labelText = variantLabel(variant);

  if (selectable) {
    tile.type = "button";
    tile.setAttribute("role", "option");
    tile.setAttribute("aria-label", labelText);
    tile.setAttribute("aria-selected", selected ? "true" : "false");
  } else {
    tile.setAttribute("aria-label", labelText);
  }

  tile.className = `alt-arts-variant-tile${selected ? " is-selected" : ""}${selectable ? " alt-arts-variant-tile--art-only" : " alt-arts-variant-tile--static"}`;
  tile.dataset.variantKey = variant.variantKey;

  appendVariantTileMedia(tile, variant);

  if (!selectable) {
    const label = document.createElement("span");
    label.className = "alt-arts-variant-tile-label";
    label.textContent = labelText;
    tile.appendChild(label);
  }

  if (selectable) {
    tile.addEventListener("click", () => {
      selectedVariantKey = variant.variantKey;
      refreshVariantPicker();
      updateItemInputUi();
    });
  }

  return tile;
}

function refreshVariantPicker() {
  const wrap = els.variantWrap();
  const picker = els.variantPicker();
  if (!wrap || !picker) return;

  const raw = String(els.itemInput()?.value || "").trim();
  const base = resolveBaseItemName(raw);
  const variants = base ? getVariantsForBase(base) : [];
  const selectable = variants.length > 1;

  picker.innerHTML = "";

  if (!variants.length) {
    wrap.hidden = true;
    selectedVariantKey = null;
    return;
  }

  wrap.hidden = false;
  picker.setAttribute("role", selectable ? "listbox" : "group");

  if (variants.length === 1) {
    selectedVariantKey = variants[0].variantKey;
  } else if (selectedVariantKey && !variants.some((v) => v.variantKey === selectedVariantKey)) {
    selectedVariantKey = null;
  }

  for (const v of variants) {
    const isSelected = selectable ? selectedVariantKey === v.variantKey : true;
    picker.appendChild(buildVariantTile(v, { selectable, selected: isSelected }));
  }
}

function updateMarketFromPayload(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const map = new Map();
  const lookup = new Map();
  const bases = new Set();

  for (const it of items) {
    const itemName = String(it.itemName || "").trim();
    const baseItemName = String(it.baseItemName || itemName).trim();
    if (!itemName) continue;

    const variantKey = variantKeyFromParts(itemName, it.imageNameFilter);
    const floorRaw = Number(it.lowestMirror ?? it.latest?.lowestMirror);
    const lowestMirror = Number.isFinite(floorRaw) && floorRaw > 0 ? floorRaw : null;
    const imagePath = it.imagePath ? String(it.imagePath) : null;

    map.set(variantKey, {
      variantKey,
      itemName,
      baseItemName,
      mode: String(it.mode || ""),
      imageNameFilter: it.imageNameFilter ? String(it.imageNameFilter) : null,
      imagePath,
      lowestMirror,
    });
    lookup.set(normalizeLookupKey(baseItemName), baseItemName);
    lookup.set(normalizeLookupKey(itemName), baseItemName);
    bases.add(baseItemName);
  }

  catalogByKey = map;
  baseNameLookup = lookup;
  baseItemNames = [...bases].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  catalogLoaded = map.size > 0;
  refreshDatalist();
  syncSelectedVariantForInput();
  refreshVariantPicker();
  updateItemInputUi();
}

function refreshDatalist() {
  const list = els.datalist();
  if (!list) return;
  list.innerHTML = "";
  for (const name of baseItemNames) {
    const opt = document.createElement("option");
    opt.value = name;
    list.appendChild(opt);
  }
}

async function fetchPrices() {
  try {
    const response = await fetch(buildPricesApiUrl(), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    updateMarketFromPayload(payload);
    setStatus("Market data updated", true);
    return true;
  } catch (err) {
    setStatus("Market data unavailable", false);
    return false;
  }
}

function renderSummary(state) {
  const root = els.summary();
  if (!root) return;

  const activeCount = state.active.length;
  const costBasis = state.active.reduce((s, l) => s + l.purchaseMirror, 0);
  const realized = state.sold.reduce((s, l) => s + l.profitMirror, 0);
  const soldCount = state.sold.length;

  root.innerHTML = "";
  const stats = [
    { label: "Active lots", value: String(activeCount) },
    { label: "Cost basis", value: `${formatMirrorDisplay(costBasis)} mirrors` },
    { label: "Realized profit", value: `${formatMirrorDisplay(realized)} mirrors` },
    { label: "Sold", value: String(soldCount) },
  ];

  for (const st of stats) {
    const wrap = document.createElement("div");
    wrap.className = "alt-arts-stat";
    const lab = document.createElement("span");
    lab.className = "alt-arts-stat-label";
    lab.textContent = st.label;
    const val = document.createElement("span");
    val.className = "alt-arts-stat-value";
    val.textContent = st.value;
    wrap.append(lab, val);
    root.appendChild(wrap);
  }
}

function appendItemIcon(parent, imagePath, itemName) {
  const img = document.createElement("img");
  img.className = "alt-arts-item-icon";
  img.src = imagePath;
  img.alt = itemName ? `${itemName} icon` : "";
  img.loading = "lazy";
  img.decoding = "async";
  parent.appendChild(img);
}

function buildItemCell(lot) {
  const cell = document.createElement("div");
  cell.className = "alt-arts-item-cell";
  const variant = typeof lot === "object" ? getVariantForLot(lot) : null;
  const displayName = variant?.itemName || String(lot?.itemName || lot || "").trim();

  if (variant?.imagePath) {
    appendItemIcon(cell, variant.imagePath, displayName);
  }
  const textWrap = document.createElement("div");
  textWrap.className = "alt-arts-item-text";
  const name = document.createElement("span");
  name.className = "alt-arts-item-name";
  name.textContent = displayName;
  textWrap.appendChild(name);
  if (variant) {
    const tag = document.createElement("span");
    tag.className = "alt-arts-variant-tag";
    tag.textContent = variantLabel(variant);
    textWrap.appendChild(tag);
  }
  cell.appendChild(textWrap);
  if (!variant && catalogLoaded) {
    const warn = document.createElement("span");
    warn.className = "alt-arts-unknown-tag";
    warn.textContent = "Unknown";
    warn.title = "Not in current catalog — delete and re-add with a valid item";
    cell.appendChild(warn);
  }
  return cell;
}

function onItemFieldChange() {
  syncSelectedVariantForInput();
  refreshVariantPicker();
  updateItemInputUi();
}

function updateItemInputUi() {
  const input = els.itemInput();
  const addBtn = els.addBtn();
  if (!input) return;

  const raw = String(input.value || "").trim();
  const base = resolveBaseItemName(raw);
  const variants = base ? getVariantsForBase(base) : [];
  const selected = getSelectedVariant();

  input.classList.remove("alt-arts-input--valid", "alt-arts-input--invalid");

  if (!raw) {
    setFormHint(catalogLoaded ? "Choose a tracked item from the list." : "Loading item catalog…");
    if (addBtn) addBtn.disabled = !catalogLoaded;
    return;
  }

  if (!catalogLoaded) {
    setFormHint("Loading item catalog…");
    if (addBtn) addBtn.disabled = true;
    return;
  }

  if (!base) {
    input.classList.add("alt-arts-input--invalid");
    setFormHint(`"${raw}" is not a tracked item. Pick one from the suggestions.`, true);
    if (addBtn) addBtn.disabled = true;
    return;
  }

  const exactMatch = variants.find((v) => normalizeLookupKey(v.itemName) === normalizeLookupKey(raw));
  if (!exactMatch && base !== raw && variants.length > 1) {
    input.value = base;
  }

  if (variants.length > 1 && !selected) {
    input.classList.add("alt-arts-input--valid");
    setFormHint("Click the art image for the variant you bought.", false);
    if (addBtn) addBtn.disabled = true;
    return;
  }

  if (!selected) {
    input.classList.add("alt-arts-input--invalid");
    setFormHint("Could not resolve item variant.", true);
    if (addBtn) addBtn.disabled = true;
    return;
  }

  input.classList.add("alt-arts-input--valid");
  setFormHint(variants.length > 1 ? `${variantLabel(selected)} selected.` : "");
  if (addBtn) addBtn.disabled = false;
}

function renderActiveTable(state) {
  const tbody = els.activeTbody();
  const emptyEl = els.activeEmpty();
  if (!tbody) return;

  tbody.innerHTML = "";

  if (!state.active.length) {
    if (emptyEl) emptyEl.hidden = false;
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  for (const lot of state.active) {
    const tr = document.createElement("tr");
    tr.dataset.lotId = lot.id;

    const tdItem = document.createElement("td");
    tdItem.appendChild(buildItemCell(lot));
    if (lot.notes) {
      const notes = document.createElement("span");
      notes.className = "alt-arts-notes";
      notes.textContent = lot.notes;
      notes.title = lot.notes;
      tdItem.appendChild(notes);
    }

    const tdPurchase = document.createElement("td");
    tdPurchase.appendChild(buildMirrorPriceNode(lot.purchaseMirror));

    const variant = getVariantForLot(lot);
    const floor = variant?.lowestMirror ?? null;

    const tdFloor = document.createElement("td");
    if (floor != null) {
      tdFloor.appendChild(buildMirrorPriceNode(floor));
    } else {
      tdFloor.textContent = "—";
    }

    const tdVs = document.createElement("td");
    if (floor != null) {
      const delta = floor - lot.purchaseMirror;
      const pill = document.createElement("span");
      pill.className = `cmp-pill ${deltaPillClass(delta)}`;
      pill.textContent = formatDeltaMirror(delta);
      tdVs.appendChild(pill);
    } else {
      tdVs.textContent = "—";
    }

    const tdDate = document.createElement("td");
    tdDate.className = "alt-arts-date";
    tdDate.textContent = formatShortDate(lot.purchasedAt);

    const tdActions = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "alt-arts-actions";

    const sellBtn = document.createElement("button");
    sellBtn.type = "button";
    sellBtn.className = "compare-btn compare-btn--ghost";
    sellBtn.textContent = sellExpandedId === lot.id ? "Cancel" : "Sell";
    sellBtn.addEventListener("click", () => {
      sellExpandedId = sellExpandedId === lot.id ? null : lot.id;
      renderAll();
    });

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "compare-btn compare-btn--ghost";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => {
      if (!window.confirm(`Remove lot "${lot.itemName}" (${formatMirrorDisplay(lot.purchaseMirror)} mirrors)?`)) {
        return;
      }
      const next = readState();
      next.active = next.active.filter((l) => l.id !== lot.id);
      if (sellExpandedId === lot.id) sellExpandedId = null;
      saveState(next);
      renderAll();
    });

    actions.append(sellBtn, delBtn);
    tdActions.appendChild(actions);

    tr.append(tdItem, tdPurchase, tdFloor, tdVs, tdDate, tdActions);
    tbody.appendChild(tr);

    if (sellExpandedId === lot.id) {
      const sellTr = document.createElement("tr");
      sellTr.className = "alt-arts-sell-row";
      const sellTd = document.createElement("td");
      sellTd.colSpan = 6;

      const panel = document.createElement("div");
      panel.className = "alt-arts-sell-panel";

      const label = document.createElement("span");
      label.className = "compare-label";
      label.textContent = "Sale price (mirrors)";

      const saleInput = document.createElement("input");
      saleInput.type = "number";
      saleInput.min = "0";
      saleInput.step = "any";
      saleInput.className = "compare-input";
      saleInput.placeholder = String(lot.purchaseMirror + 1);

      const hint = document.createElement("span");
      hint.className = "compare-label";
      hint.textContent = `Bought at ${formatMirrorDisplay(lot.purchaseMirror)}`;

      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "compare-btn";
      confirmBtn.textContent = "Confirm sale";

      const updateConfirm = () => {
        const sale = Number(saleInput.value);
        confirmBtn.disabled = !Number.isFinite(sale) || sale < 0;
      };
      saleInput.addEventListener("input", updateConfirm);
      updateConfirm();

      confirmBtn.addEventListener("click", () => {
        const saleMirror = Number(saleInput.value);
        if (!Number.isFinite(saleMirror) || saleMirror < 0) {
          return;
        }
        const next = readState();
        const idx = next.active.findIndex((l) => l.id === lot.id);
        if (idx < 0) return;
        const removed = next.active.splice(idx, 1)[0];
        const profitMirror = saleMirror - removed.purchaseMirror;
        next.sold.push({
          id: removed.id,
          itemName: removed.itemName,
          variantKey: removed.variantKey,
          baseItemName: removed.baseItemName,
          purchaseMirror: removed.purchaseMirror,
          saleMirror,
          profitMirror,
          purchasedAt: removed.purchasedAt,
          soldAt: new Date().toISOString(),
          notes: removed.notes,
        });
        sellExpandedId = null;
        saveState(next);
        renderAll();
      });

      panel.append(label, saleInput, hint, confirmBtn);
      sellTd.appendChild(panel);
      sellTr.appendChild(sellTd);
      tbody.appendChild(sellTr);
    }
  }
}

function renderSoldTable(state) {
  const tbody = els.soldTbody();
  const emptyEl = els.soldEmpty();
  const countEl = els.soldCount();
  if (!tbody) return;

  if (countEl) {
    countEl.textContent = state.sold.length ? `(${state.sold.length})` : "";
  }

  tbody.innerHTML = "";

  if (!state.sold.length) {
    if (emptyEl) emptyEl.hidden = false;
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  const sorted = [...state.sold].sort(
    (a, b) => new Date(b.soldAt).getTime() - new Date(a.soldAt).getTime(),
  );

  for (const lot of sorted) {
    const tr = document.createElement("tr");

    const tdItem = document.createElement("td");
    tdItem.appendChild(buildItemCell(lot));

    const tdPurchase = document.createElement("td");
    tdPurchase.appendChild(buildMirrorPriceNode(lot.purchaseMirror));

    const tdSale = document.createElement("td");
    tdSale.appendChild(buildMirrorPriceNode(lot.saleMirror));

    const tdProfit = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `cmp-pill ${deltaPillClass(lot.profitMirror)}`;
    pill.textContent = formatDeltaMirror(lot.profitMirror);
    tdProfit.appendChild(pill);

    const tdSold = document.createElement("td");
    tdSold.className = "alt-arts-date";
    tdSold.textContent = formatShortDate(lot.soldAt);

    const tdActions = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "alt-arts-actions";

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "compare-btn compare-btn--ghost";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => {
      const profitLabel = formatDeltaMirror(lot.profitMirror);
      if (
        !window.confirm(
          `Remove sold record for "${lot.itemName}" (${formatMirrorDisplay(lot.purchaseMirror)} → ${formatMirrorDisplay(lot.saleMirror)}, profit ${profitLabel})?`,
        )
      ) {
        return;
      }
      const next = readState();
      next.sold = next.sold.filter((l) => l.id !== lot.id);
      saveState(next);
      renderAll();
    });

    actions.appendChild(delBtn);
    tdActions.appendChild(actions);

    tr.append(tdItem, tdPurchase, tdSale, tdProfit, tdSold, tdActions);
    tbody.appendChild(tr);
  }
}

function renderAll() {
  const state = readState();
  renderSummary(state);
  renderActiveTable(state);
  renderSoldTable(state);
}

function handleAddSubmit(event) {
  event.preventDefault();
  const rawName = String(els.itemInput()?.value || "").trim();
  const variant = getSelectedVariant();
  const purchaseMirror = Number(els.purchaseInput()?.value);
  const purchasedAt = parsePurchaseDateInput(els.dateInput()?.value);
  const notes = String(els.notesInput()?.value || "").trim();

  if (!catalogLoaded) {
    setFormHint("Item catalog still loading — try again in a moment.", true);
    return;
  }
  if (!rawName) {
    setFormHint("Enter an item name.", true);
    return;
  }
  if (!resolveBaseItemName(rawName)) {
    setFormHint(`"${rawName}" is not a tracked item. Pick one from the suggestions.`, true);
    updateItemInputUi();
    return;
  }
  if (!variant) {
    setFormHint("Click the art image for the variant you bought.", true);
    updateItemInputUi();
    return;
  }
  if (!Number.isFinite(purchaseMirror) || purchaseMirror < 0) {
    setFormHint("Enter a valid purchase price in mirrors.", true);
    return;
  }

  const state = readState();
  state.active.push({
    id: newId(),
    itemName: variant.itemName,
    variantKey: variant.variantKey,
    baseItemName: variant.baseItemName,
    purchaseMirror,
    purchasedAt,
    notes,
  });
  saveState(state);
  setFormHint("");
  selectedVariantKey = null;

  const form = els.addForm();
  if (form) form.reset();
  refreshVariantPicker();
  updateItemInputUi();
  renderAll();
}

export function initAltArtsHoldings() {
  const form = els.addForm();
  if (form) {
    form.addEventListener("submit", handleAddSubmit);
  }
  const itemInput = els.itemInput();
  if (itemInput) {
    itemInput.addEventListener("input", onItemFieldChange);
    itemInput.addEventListener("change", onItemFieldChange);
  }
  updateItemInputUi();
  renderAll();
  fetchPrices().then(() => {
    updateItemInputUi();
    renderAll();
  });
  setInterval(async () => {
    await fetchPrices();
    updateItemInputUi();
    renderAll();
  }, PRICES_REFRESH_MS);
}
