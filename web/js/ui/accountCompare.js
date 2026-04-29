const STORAGE_KEY = "pmf.accountCompare.v1";
const DEFAULT_ACCOUNTS = ["ABVT#0013", "junglechrist#0894"];

function readState() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { accounts: [...DEFAULT_ACCOUNTS], mode: "all" };
    }
    const parsed = JSON.parse(raw);
    const accounts = Array.isArray(parsed?.accounts)
      ? parsed.accounts.map((s) => String(s || "").trim()).filter(Boolean)
      : (typeof parsed?.accounts === "string"
        ? String(parsed.accounts).split(",").map((s) => s.trim()).filter(Boolean)
        : [...DEFAULT_ACCOUNTS]);
    const mode = parsed?.mode === "diff" ? "diff" : "all";
    return { accounts: accounts.length ? accounts : [...DEFAULT_ACCOUNTS], mode };
  } catch {
    return { accounts: [...DEFAULT_ACCOUNTS], mode: "all" };
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

async function fetchCompare({ accounts, mode }) {
  const params = new URLSearchParams();
  params.set("accounts", accounts.join(","));
  params.set("mode", mode || "all");
  const res = await fetch(`/api/account-compare?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function renderHeader(thead, accounts) {
  thead.innerHTML = "";
  const tr = document.createElement("tr");
  const mk = (text) => {
    const th = document.createElement("th");
    th.textContent = text;
    return th;
  };
  tr.appendChild(mk("Item"));
  tr.appendChild(mk("Market (lowest)"));
  accounts.forEach((a) => tr.appendChild(mk(a)));
  thead.appendChild(tr);
}

function buildCellPill({ amount, delta }) {
  const cls = pillClass(delta);
  const pill = document.createElement("span");
  pill.className = `cmp-pill ${cls}`.trim();

  const price = document.createElement("span");
  price.className = "cmp-price";
  price.textContent = amount == null ? "—" : `${formatMirror(amount)} m`;
  pill.appendChild(price);

  if (amount != null && delta != null && Number.isFinite(Number(delta)) && Math.abs(Number(delta)) > 1e-9) {
    const d = document.createElement("span");
    d.className = "cmp-delta";
    const sign = Number(delta) > 0 ? "+" : "";
    d.textContent = `${sign}${formatMirror(delta)} m`;
    pill.appendChild(d);
  }

  return pill;
}

function renderBody(tbody, payload) {
  tbody.innerHTML = "";
  const accounts = Array.isArray(payload?.accounts) ? payload.accounts : [];
  const rows = Array.isArray(payload?.rows) ? payload.rows : [];

  const normalized = rows.map((r) => {
    const market = r?.market?.amount != null ? Number(r.market.amount) : null;
    const acct = r?.accounts && typeof r.accounts === "object" ? r.accounts : {};
    const deltas = accounts.map((a) => {
      const v = acct?.[a];
      const amt = v != null ? Number(v) : null;
      const delta = market != null && amt != null ? amt - market : null;
      return { account: a, amount: amt, delta };
    });
    // Sort: worst overpriced first, then missing.
    const worst = deltas.reduce((acc, d) => {
      if (d.delta == null) return acc;
      if (acc == null) return d.delta;
      return Math.max(acc, d.delta);
    }, null);
    const hasAny = deltas.some((d) => d.amount != null);
    return { raw: r, market, deltas, worstDelta: worst, hasAny };
  });

  normalized.sort((a, b) => {
    const aw = a.worstDelta;
    const bw = b.worstDelta;
    if (aw == null && bw != null) return 1;
    if (aw != null && bw == null) return -1;
    if (aw != null && bw != null && aw !== bw) return bw - aw; // desc
    // fall back: cheapest market first
    const am = a.market;
    const bm = b.market;
    if (am == null && bm != null) return 1;
    if (am != null && bm == null) return -1;
    if (am != null && bm != null && am !== bm) return am - bm;
    return String(a.raw?.displayName || "").localeCompare(String(b.raw?.displayName || ""));
  });

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
    tdMarket.appendChild(buildCellPill({ amount: row.market, delta: 0 }));
    tr.appendChild(tdMarket);

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
  const btn = document.getElementById("accountCompareRefresh");
  const summaryEl = document.getElementById("accountCompareSummary");
  const hintEl = document.getElementById("accountCompareHint");
  const thead = document.getElementById("accountCompareThead");
  const tbody = document.getElementById("accountCompareTbody");

  if (!panel || !chipsEl || !inputEl || !addBtn || !modeEl || !btn || !summaryEl || !hintEl || !thead || !tbody) {
    return;
  }

  let state = readState();
  state.accounts = normalizeAccounts(state.accounts);
  modeEl.value = state.mode;

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
        saveState({ accounts: state.accounts, mode: state.mode });
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
    saveState({ accounts: state.accounts, mode: state.mode });
    renderChips();
    return true;
  };

  const run = async (options = {}) => {
    const accounts = normalizeAccounts(state.accounts);
    const mode = modeEl.value === "diff" ? "diff" : "all";
    state.mode = mode;
    saveState({ accounts, mode });

    setBusy(true);
    hintEl.textContent = "";
    try {
      const payload = await fetchCompare({ accounts, mode });
      if (!payload?.ok) {
        throw new Error(payload?.error || "Failed to load compare data");
      }
      renderHeader(thead, payload.accounts || accounts);
      renderBody(tbody, payload);
      renderSummary(summaryEl, hintEl, payload);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      summaryEl.textContent = "Account compare unavailable.";
      hintEl.textContent = msg;
      thead.innerHTML = "";
      tbody.innerHTML = "";
    } finally {
      setBusy(false);
    }
  };

  btn.addEventListener("click", () => void run({ force: true }));
  modeEl.addEventListener("change", () => void run());

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
      saveState({ accounts: state.accounts, mode: state.mode });
      renderChips();
      void run();
    }
  });

  // Initial load.
  renderChips();
  void run();
}

