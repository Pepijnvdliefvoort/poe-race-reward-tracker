const STORAGE_KEY = "pmf.accountCompare.v1";
const DEFAULT_ACCOUNTS = ["ABVT#0013", "junglechrist#0894"];

function readState() {
  const defaults = () => ({
    accounts: [...DEFAULT_ACCOUNTS],
    mode: "all",
    sortCol: null,
    sortDir: /** @type {'asc' | 'desc'} */ ("asc"),
    topN: 5,
  });
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return defaults();
    }
    const parsed = JSON.parse(raw);
    const accounts = Array.isArray(parsed?.accounts)
      ? parsed.accounts.map((s) => String(s || "").trim()).filter(Boolean)
      : (typeof parsed?.accounts === "string"
        ? String(parsed.accounts).split(",").map((s) => s.trim()).filter(Boolean)
        : [...DEFAULT_ACCOUNTS]);
    const mode = parsed?.mode === "diff" ? "diff" : "all";

    let sortCol = null;
    if (parsed && Object.prototype.hasOwnProperty.call(parsed, "sortCol")) {
      if (parsed.sortCol === null) sortCol = null;
      else {
        const n = Number(parsed.sortCol);
        if (Number.isFinite(n)) sortCol = n;
      }
    }
    const sortDir = parsed?.sortDir === "desc" ? "desc" : "asc";
    let topN = 5;
    if (parsed && Object.prototype.hasOwnProperty.call(parsed, "topN")) {
      const n = Number(parsed.topN);
      if (Number.isFinite(n)) topN = Math.min(10, Math.max(1, Math.floor(n)));
    }

    return {
      accounts: accounts.length ? accounts : [...DEFAULT_ACCOUNTS],
      mode,
      sortCol,
      sortDir,
      topN,
    };
  } catch {
    return defaults();
  }
}

function saveState(state) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // ignore
  }
}

function normalizeAccounts(accounts) {
  const out = [];
  const seen = new Set();
  for (const a of accounts || []) {
    const s = String(a || "").trim();
    if (!s) continue;
    if (seen.has(s)) continue;
    seen.add(s);
    out.push(s);
    if (out.length >= 6) break;
  }
  return out;
}

function splitAccountsFromText(raw) {
  return String(raw || "")
    .replace(/\n/g, ",")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function formatMirror(v) {
  if (v == null) return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return "";
  if (Math.abs(n - Math.round(n)) < 1e-9) return String(Math.round(n));
  return n.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
}

function buildMirrorPriceNode(v) {
  const wrap = document.createElement("span");
  wrap.className = "cmp-price-wrap";

  const amountEl = document.createElement("span");
  amountEl.className = "cmp-price-amount";
  amountEl.textContent = formatMirror(v);

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

function pillClass(delta) {
  if (delta == null) return "cmp-missing";
  const d = Number(delta);
  if (!Number.isFinite(d)) return "cmp-missing";
  if (Math.abs(d) < 1e-9) return "cmp-eq";
  // Negative delta means account is cheaper than market (good).
  return d < 0 ? "cmp-ok" : "cmp-bad";
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function fetchCompare({ accounts, mode, topN }) {
  const params = new URLSearchParams();
  params.set("accounts", accounts.join(","));
  params.set("mode", mode || "all");
  if (topN != null) {
    params.set("topN", String(topN));
  }
  const res = await fetch(`/api/account-compare?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** Column index in thead / sort keys: 0 item name, 1 market, 2+ account columns in order */
const COL_ITEM = 0;
const COL_MARKET = 1;

function clampSortState(sortState, accounts) {
  if (sortState.col == null) return;
  const maxCol = 1 + accounts.length;
  if (sortState.col < COL_ITEM || sortState.col > maxCol) {
    sortState.col = null;
    sortState.dir = "asc";
  }
}

function defaultNormalizedComparator(a, b) {
  const aw = a.worstDelta;
  const bw = b.worstDelta;
  if (aw == null && bw != null) return 1;
  if (aw != null && bw == null) return -1;
  if (aw != null && bw != null && aw !== bw) return bw - aw;
  const am = a.market;
  const bm = b.market;
  if (am == null && bm != null) return 1;
  if (am != null && bm == null) return -1;
  if (am != null && bm != null && am !== bm) return am - bm;
  return String(a.raw?.displayName || "").localeCompare(String(b.raw?.displayName || ""));
}

function sortKeyForColumn(row, colIndex, accounts) {
  if (colIndex === COL_ITEM) return String(row.raw?.displayName || row.raw?.itemName || "");
  if (colIndex === COL_MARKET) return row.market;
  const acctIdx = colIndex - 2;
  if (acctIdx >= 0 && acctIdx < accounts.length) {
    const amt = row.deltas[acctIdx]?.amount;
    return amt != null && Number.isFinite(Number(amt)) ? Number(amt) : null;
  }
  return null;
}

function nameTieBreak(a, b) {
  return String(a.raw?.displayName || "").localeCompare(String(b.raw?.displayName || ""));
}

/**
 * @param {Array} normalized from buildNormalizedRows
 * @param {string[]} accounts
 * @param {{ col: number | null, dir: 'asc' | 'desc' }} sortState
 */
function sortNormalizedRows(normalized, accounts, sortState) {
  const out = [...normalized];
  if (sortState.col == null) {
    out.sort(defaultNormalizedComparator);
    return out;
  }

  const col = sortState.col;
  const asc = sortState.dir === "asc";

  out.sort((a, b) => {
    if (col === COL_ITEM) {
      const sa = sortKeyForColumn(a, col, accounts);
      const sb = sortKeyForColumn(b, col, accounts);
      const c = String(sa).localeCompare(String(sb));
      if (c !== 0) return asc ? c : -c;
      return nameTieBreak(a, b);
    }

    const na = sortKeyForColumn(a, col, accounts);
    const nb = sortKeyForColumn(b, col, accounts);
    const aMiss = na == null || !Number.isFinite(Number(na));
    const bMiss = nb == null || !Number.isFinite(Number(nb));
    if (aMiss && bMiss) return nameTieBreak(a, b);
    if (aMiss) return 1;
    if (bMiss) return -1;
    const cmp = Number(na) - Number(nb);
    if (cmp !== 0) return asc ? cmp : -cmp;
    return nameTieBreak(a, b);
  });

  return out;
}

function buildNormalizedRows(payload) {
  const accounts = Array.isArray(payload?.accounts) ? payload.accounts : [];
  const rows = Array.isArray(payload?.rows) ? payload.rows : [];
  const EPS = 1e-9;

  return rows.map((r) => {
    const market = r?.market?.amount != null ? Number(r.market.amount) : null;
    const marketExcl = r?.market?.excluding && typeof r.market.excluding === "object" ? r.market.excluding : {};
    const acct = r?.accounts && typeof r.accounts === "object" ? r.accounts : {};
    const deltas = accounts.map((a) => {
      const v = acct?.[a];
      const amt = v != null ? Number(v) : null;
      let delta = market != null && amt != null ? amt - market : null;

      if (
        market != null &&
        amt != null &&
        delta != null &&
        Number.isFinite(Number(delta)) &&
        Math.abs(Number(delta)) < EPS
      ) {
        const exclRaw = marketExcl?.[a];
        const excl = exclRaw != null ? Number(exclRaw) : null;
        if (excl != null && Number.isFinite(excl) && excl - amt > EPS) {
          delta = amt - excl;
        }
      }
      return { account: a, amount: amt, delta };
    });
    const worst = deltas.reduce((acc, d) => {
      if (d.delta == null) return acc;
      if (acc == null) return d.delta;
      return Math.max(acc, d.delta);
    }, null);
    const hasAny = deltas.some((d) => d.amount != null);
    const marketTopListings = Array.isArray(r?.market?.topListings) ? r.market.topListings : [];
    return { raw: r, market, marketTopListings, deltas, worstDelta: worst, hasAny };
  });
}

function renderCompareHeader(thead, accounts, sortState) {
  thead.innerHTML = "";
  const tr = document.createElement("tr");

  const mkSortable = (text, colIndex) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.dataset.sortCol = String(colIndex);
    th.className = "compare-th compare-th--sortable";
    th.tabIndex = 0;
    th.title =
      colIndex === COL_ITEM
        ? "Sort by item name"
        : colIndex === COL_MARKET
          ? "Sort by cheapest listing (floor)"
          : "Sort by price";

    const label = document.createElement("span");
    label.className = "compare-th-label";
    label.textContent = text;
    th.appendChild(label);

    const ind = document.createElement("span");
    ind.className = "compare-th-sort-ind";
    ind.setAttribute("aria-hidden", "true");
    if (sortState.col === colIndex) {
      ind.textContent = sortState.dir === "asc" ? " ▲" : " ▼";
      th.setAttribute("aria-sort", sortState.dir === "asc" ? "ascending" : "descending");
    } else {
      ind.textContent = "";
      th.setAttribute("aria-sort", "none");
    }
    th.appendChild(ind);

    return th;
  };

  tr.appendChild(mkSortable("Item", COL_ITEM));
  const nLabel = Number.isFinite(Number(sortState?.topN)) ? Math.min(10, Math.max(1, Math.floor(Number(sortState.topN)))) : 5;
  tr.appendChild(mkSortable(`Market (top ${nLabel})`, COL_MARKET));
  accounts.forEach((a, i) => tr.appendChild(mkSortable(a, 2 + i)));
  thead.appendChild(tr);
}

function buildCellPill({ amount, delta }) {
  const cls = pillClass(delta);
  const pill = document.createElement("span");
  pill.className = `cmp-pill ${cls}`.trim();

  const price = document.createElement("span");
  price.className = "cmp-price";
  if (amount == null) price.textContent = "—";
  else price.appendChild(buildMirrorPriceNode(amount));
  pill.appendChild(price);

  if (amount != null && delta != null && Number.isFinite(Number(delta)) && Math.abs(Number(delta)) > 1e-9) {
    const d = document.createElement("span");
    d.className = "cmp-delta";
    const sign = Number(delta) > 0 ? "+" : "";
    d.textContent = `${sign}${formatMirror(delta)}`;
    pill.appendChild(d);
  }

  return pill;
}

function filterMarketTopListings(topListings) {
  return Array.isArray(topListings)
    ? topListings.filter((x) => x != null && Number.isFinite(Number(x.mirrorEquiv)))
    : [];
}

function buildMarketColumnCell(topListings, fallbackAmount, maxN) {
  const wrap = document.createElement("div");
  wrap.className = "compare-market-stack";
  const list = filterMarketTopListings(topListings);

  if (!list.length) {
    wrap.appendChild(buildCellPill({ amount: fallbackAmount, delta: 0 }));
    return wrap;
  }

  const limit = Number.isFinite(Number(maxN)) ? Math.min(10, Math.max(1, Math.floor(Number(maxN)))) : 5;
  const pills = list.slice(0, limit);

  const makeMarketPill = (entry) => {
    const pill = document.createElement("span");
    const instant = !!entry?.instantBuyout;
    pill.className = `cmp-pill cmp-pill--market-row ${instant ? "cmp-market-instant" : "cmp-market-inperson"}`.trim();
    const m = Number(entry.mirrorEquiv);
    const price = document.createElement("span");
    price.className = "cmp-price";
    price.appendChild(buildMirrorPriceNode(m));

    const seller = document.createElement("span");
    seller.className = "compare-market-seller";
    const name = entry.sellerName != null ? String(entry.sellerName) : "";
    seller.textContent = name;
    seller.title = name;

    const mode = instant ? "Instant" : "In-person";
    const cur = entry.listingCurrency != null ? String(entry.listingCurrency).trim() : "";
    const amt = entry.listingAmount != null ? Number(entry.listingAmount) : null;
    const detail =
      cur && amt != null && Number.isFinite(amt) && !/^mirror/i.test(cur) ? `${formatMirror(amt)} ${cur}` : "";
    pill.title = [name || null, mode, detail || null].filter(Boolean).join(" · ");

    pill.appendChild(price);
    pill.appendChild(seller);
    return pill;
  };

  const firstRow = document.createElement("div");
  firstRow.className = "compare-market-first-row";
  const firstPill = makeMarketPill(pills[0]);
  firstRow.appendChild(firstPill);
  if (pills[0]?.corrupted) {
    const c = document.createElement("span");
    c.className = "compare-market-corrupt compare-market-corrupt--outside";
    c.textContent = "C";
    c.setAttribute("aria-label", "Corrupted");
    c.title = "Corrupted";
    firstRow.appendChild(c);
  }
  if (pills.length > 1) {
    const hint = document.createElement("span");
    hint.className = "compare-market-collapsed-hint";
    hint.textContent = `+${pills.length - 1} more`;
    hint.setAttribute("aria-hidden", "true");
    firstRow.appendChild(hint);
  }
  wrap.appendChild(firstRow);

  for (let i = 1; i < pills.length; i++) {
    const row = document.createElement("div");
    row.className = "compare-market-line";
    row.appendChild(makeMarketPill(pills[i]));
    if (pills[i]?.corrupted) {
      const c = document.createElement("span");
      c.className = "compare-market-corrupt compare-market-corrupt--outside";
      c.textContent = "C";
      c.setAttribute("aria-label", "Corrupted");
      c.title = "Corrupted";
      row.appendChild(c);
    }
    wrap.appendChild(row);
  }

  return wrap;
}

function renderCompareBody(tbody, normalized, topN) {
  tbody.innerHTML = "";

  for (const row of normalized) {
    const r = row.raw || {};
    const tr = document.createElement("tr");

    const tdItem = document.createElement("td");
    const itemWrap = document.createElement("div");
    itemWrap.className = "compare-item";
    const name = document.createElement("div");
    name.className = "compare-item-name";
    name.textContent = r.displayName || r.itemName || "Unknown";
    itemWrap.append(name);
    tdItem.appendChild(itemWrap);
    tr.appendChild(tdItem);

    const tdMarket = document.createElement("td");
    tdMarket.className = "compare-td-market";
    tdMarket.appendChild(buildMarketColumnCell(row.marketTopListings, row.market, topN));
    tr.appendChild(tdMarket);

    const marketList = filterMarketTopListings(row.marketTopListings);
    if (marketList.length > 1) {
      tr.classList.add("compare-row--market-collapsible");
      tr.tabIndex = 0;
      tr.title = "Hover row to preview market listings";
    }

    for (const d of row.deltas) {
      const td = document.createElement("td");
      td.appendChild(buildCellPill({ amount: d.amount, delta: d.delta }));
      tr.appendChild(td);
    }

    tbody.appendChild(tr);
  }
}

function renderSummary(summaryEl, hintEl, payload) {
  const accounts = Array.isArray(payload?.accounts) ? payload.accounts : [];
  const rows = Array.isArray(payload?.rows) ? payload.rows : [];

  let missing = 0;
  let overpriced = 0;
  let undercut = 0;
  let equal = 0;

  for (const r of rows) {
    const market = r?.market?.amount != null ? Number(r.market.amount) : null;
    const acct = r?.accounts && typeof r.accounts === "object" ? r.accounts : {};
    for (const a of accounts) {
      const v = acct?.[a];
      const amt = v != null ? Number(v) : null;
      if (amt == null || !Number.isFinite(amt)) {
        missing += 1;
        continue;
      }
      if (market == null || !Number.isFinite(market)) {
        equal += 1;
        continue;
      }
      const delta = amt - market;
      if (Math.abs(delta) < 1e-9) equal += 1;
      else if (delta > 0) overpriced += 1;
      else undercut += 1;
    }
  }

  summaryEl.textContent =
    `${rows.length} items · ${accounts.length} account(s) · ` +
    `overpriced ${overpriced} · undercut ${undercut} · equal ${equal} · missing ${missing}`;
  hintEl.textContent = "";
}

export function initAccountCompare() {
  const panel = document.getElementById("accountComparePanel");
  const chipsEl = document.getElementById("accountCompareAccountsChips");
  const inputEl = document.getElementById("accountCompareAccountInput");
  const addBtn = document.getElementById("accountCompareAddAccount");
  const modeEl = document.getElementById("accountCompareMode");
  const topNEl = document.getElementById("accountCompareTopN");
  const btn = document.getElementById("accountCompareRefresh");
  const summaryEl = document.getElementById("accountCompareSummary");
  const hintEl = document.getElementById("accountCompareHint");
  const thead = document.getElementById("accountCompareThead");
  const tbody = document.getElementById("accountCompareTbody");

  if (!panel || !chipsEl || !inputEl || !addBtn || !modeEl || !topNEl || !btn || !summaryEl || !hintEl || !thead || !tbody) {
    return;
  }

  // Market rows expand only while pointer is held down.
  if (!tbody.dataset.compareMarketRowPressExpandBound) {
    tbody.dataset.compareMarketRowPressExpandBound = "1";

    let activeRow = null;
    let activePointerId = null;
    let mouseDown = false;

    const collapseAllRows = () => {
      // Collapse any expanded rows, even if we lost track of which one.
      tbody.querySelectorAll("tr.compare-row-expanded").forEach((tr) => tr.classList.remove("compare-row-expanded"));
      // If something inside the table is focused, blur it so focus state can't "pin" expanded styles.
      const ae = document.activeElement;
      if (ae && tbody.contains(ae) && typeof ae.blur === "function") {
        ae.blur();
      }
      activeRow = null;
      activePointerId = null;
      mouseDown = false;
    };

    const clearActive = () => {
      if (activeRow) {
        activeRow.classList.remove("compare-row-expanded");
        activeRow = null;
      }
      activePointerId = null;
    };

    // Mouse: expand on mousedown, collapse on mouseup anywhere.
    let lastButtons = 0;

    document.addEventListener(
      "mousemove",
      (e) => {
        if (!mouseDown) return;
        if (typeof e.buttons === "number") lastButtons = e.buttons;
      },
      true
    );

    tbody.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const tr = e.target.closest("tr.compare-row--market-collapsible");
      if (!tr || !tbody.contains(tr)) return;
      // Prevent focus from sticking to the row on click-and-hold.
      e.preventDefault();
      clearActive();
      mouseDown = true;
      lastButtons = typeof e.buttons === "number" ? e.buttons : 1;
      activeRow = tr;
      tr.classList.add("compare-row-expanded");

      // Some environments miss mouseup (e.g. drag, iframe, lost focus). While the mouse is held,
      // poll `buttons` (via mousemove updates) and collapse immediately once it's released.
      const step = () => {
        if (!mouseDown) return;
        const held = (lastButtons & 1) === 1;
        if (!held) {
          mouseDown = false;
          clearActive();
          return;
        }
        window.requestAnimationFrame(step);
      };
      window.requestAnimationFrame(step);
    });

    window.addEventListener(
      "mouseup",
      () => {
        if (!mouseDown) return;
        mouseDown = false;
        collapseAllRows();
      },
      true
    );

    // Pointer (touch/pen): expand on pointerdown, collapse on pointerup anywhere.
    tbody.addEventListener("pointerdown", (e) => {
      const tr = e.target.closest("tr.compare-row--market-collapsible");
      if (!tr || !tbody.contains(tr)) return;
      // Only left-click / primary pointer; ignore right click.
      if (e.button != null && e.button !== 0) return;
      // Mouse is handled by mousedown/mouseup to avoid browser-specific pointerup quirks.
      if (e.pointerType === "mouse") return;
      clearActive();
      activeRow = tr;
      activePointerId = e.pointerId;
      tr.classList.add("compare-row-expanded");
      // Capture so we still get pointerup even if cursor leaves row.
      try {
        // Capture on the actual event target for best browser support.
        e.target?.setPointerCapture?.(e.pointerId);
      } catch {
        // ignore
      }
    });

    // Close on any pointerup anywhere (even outside tbody).
    // Use capture so we run before other handlers.
    window.addEventListener(
      "pointerup",
      (e) => {
        if (activePointerId == null || e.pointerId !== activePointerId) return;
        collapseAllRows();
      },
      true
    );

    window.addEventListener(
      "pointercancel",
      (e) => {
        if (activePointerId == null || e.pointerId !== activePointerId) return;
        collapseAllRows();
      },
      true
    );

    // Safety: if something steals capture / focus, collapse.
    window.addEventListener("blur", collapseAllRows);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") collapseAllRows();
    });

    // Hard safety net: if we ever see "no left button held", collapse everything.
    // This covers edge cases where mouseup doesn't fire (dragging, iframe focus shifts, etc).
    const collapseIfNoLeftButton = (buttons) => {
      if (typeof buttons !== "number") return;
      if ((buttons & 1) === 0) {
        collapseAllRows();
      }
    };

    window.addEventListener(
      "mousemove",
      (e) => {
        if (!mouseDown) return;
        collapseIfNoLeftButton(e.buttons);
      },
      true
    );

    window.addEventListener(
      "pointermove",
      (e) => {
        if (e.pointerType !== "mouse") return;
        if (!mouseDown) return;
        collapseIfNoLeftButton(e.buttons);
      },
      true
    );

    window.addEventListener("dragend", collapseAllRows, true);
  }

  let lastPayload = null;

  let state = readState();
  state.accounts = normalizeAccounts(state.accounts);
  modeEl.value = state.mode;
  topNEl.value = String(state.topN || 5);

  let sortState = {
    col: state.sortCol,
    dir: state.sortDir === "desc" ? "desc" : "asc",
    topN: state.topN || 5,
  };

  const saveComparePage = () => {
    saveState({
      accounts: state.accounts,
      mode: state.mode,
      sortCol: sortState.col,
      sortDir: sortState.dir,
      topN: sortState.topN,
    });
  };

  const redrawCompareTable = () => {
    if (!lastPayload?.ok) return;
    const accounts = Array.isArray(lastPayload.accounts) ? lastPayload.accounts : [];
    clampSortState(sortState, accounts);
    const effectiveTopN = Number.isFinite(Number(lastPayload.topN))
      ? Number(lastPayload.topN)
      : (Number.isFinite(Number(sortState.topN)) ? Number(sortState.topN) : 5);
    sortState.topN = effectiveTopN;
    renderCompareHeader(thead, accounts, sortState);
    const normalized = sortNormalizedRows(buildNormalizedRows(lastPayload), accounts, sortState);
    renderCompareBody(tbody, normalized, effectiveTopN);
  };

  const toggleSortFromHeader = (th) => {
    const col = Number(th.dataset.sortCol);
    if (!Number.isFinite(col)) return;
    if (sortState.col === col) {
      sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
    } else {
      sortState.col = col;
      sortState.dir = "asc";
    }
    redrawCompareTable();
    saveComparePage();
  };

  thead.addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort-col]");
    if (!th || !thead.contains(th)) return;
    toggleSortFromHeader(th);
  });

  thead.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const th = e.target.closest("th[data-sort-col]");
    if (!th || !thead.contains(th)) return;
    e.preventDefault();
    toggleSortFromHeader(th);
  });

  const setBusy = (busy) => {
    btn.disabled = !!busy;
    btn.textContent = busy ? "Loading…" : "Refresh";
  };

  const renderChips = () => {
    chipsEl.innerHTML = "";
    const accounts = normalizeAccounts(state.accounts);
    if (!accounts.length) {
      const empty = document.createElement("span");
      empty.className = "compare-item-sub";
      empty.textContent = "No accounts added.";
      chipsEl.appendChild(empty);
      return;
    }
    for (const a of accounts) {
      const chip = document.createElement("span");
      chip.className = "compare-chip";
      const label = document.createElement("span");
      label.textContent = a;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${a}`);
      remove.title = "Remove";
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        state.accounts = normalizeAccounts(state.accounts).filter((x) => x !== a);
        saveComparePage();
        renderChips();
        void run();
      });
      chip.append(label, remove);
      chipsEl.appendChild(chip);
    }
  };

  const addAccountsFromText = (raw) => {
    const parts = splitAccountsFromText(raw);
    if (!parts.length) return false;
    state.accounts = normalizeAccounts([...(state.accounts || []), ...parts]);
    saveComparePage();
    renderChips();
    return true;
  };

  const run = async (options = {}) => {
    const accounts = normalizeAccounts(state.accounts);
    const mode = modeEl.value === "diff" ? "diff" : "all";
    state.mode = mode;
    const topNRaw = Number(topNEl.value);
    sortState.topN = Number.isFinite(topNRaw) ? Math.min(10, Math.max(1, Math.floor(topNRaw))) : (sortState.topN || 5);
    saveComparePage();

    setBusy(true);
    hintEl.textContent = "";
    try {
      const payload = await fetchCompare({ accounts, mode, topN: sortState.topN });
      if (!payload?.ok) {
        throw new Error(payload?.error || "Failed to load compare data");
      }
      lastPayload = payload;
      clampSortState(sortState, Array.isArray(payload.accounts) ? payload.accounts : []);
      redrawCompareTable();
      saveComparePage();
      renderSummary(summaryEl, hintEl, payload);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      summaryEl.textContent = "Account compare unavailable.";
      hintEl.textContent = msg;
      lastPayload = null;
      thead.innerHTML = "";
      tbody.innerHTML = "";
    } finally {
      setBusy(false);
    }
  };

  btn.addEventListener("click", () => void run({ force: true }));
  modeEl.addEventListener("change", () => void run());
  topNEl.addEventListener("change", () => void run());

  addBtn.addEventListener("click", () => {
    const raw = inputEl.value || "";
    if (!addAccountsFromText(raw)) return;
    inputEl.value = "";
    void run();
  });

  inputEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      addBtn.click();
    }
  });

  inputEl.addEventListener("paste", (ev) => {
    const text = ev.clipboardData?.getData("text") || "";
    // If user pasted multiple accounts, absorb them into chips.
    if (text && (text.includes(",") || text.includes("\n"))) {
      ev.preventDefault();
      addAccountsFromText(text);
      inputEl.value = "";
      void run();
    }
  });

  // Allow quick backspace to remove the last chip when input is empty.
  inputEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Backspace" && !String(inputEl.value || "").trim()) {
      const accounts = normalizeAccounts(state.accounts);
      if (!accounts.length) return;
      state.accounts = accounts.slice(0, accounts.length - 1);
      saveComparePage();
      renderChips();
      void run();
    }
  });

  // Initial load.
  renderChips();
  void run();
}

