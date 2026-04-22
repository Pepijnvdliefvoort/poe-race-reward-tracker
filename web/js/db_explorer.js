const fetchOpts = { credentials: "same-origin" };

async function fetchJson(path, init) {
  const res = await fetch(path, { ...fetchOpts, ...(init || {}) });
  if (res.status === 403) {
    const err = new Error("Forbidden");
    err.status = 403;
    throw err;
  }
  if (res.status === 429) {
    const ra = res.headers.get("Retry-After");
    const err = new Error(
      ra
        ? `Too many failed authentication attempts. Retry after ${ra}s.`
        : "Too many failed authentication attempts. Try again later.",
    );
    err.status = 429;
    throw err;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function adminEndpointErrorMessage(err, label) {
  if (err?.status === 403) return "Unauthorized";
  if (err?.status === 429) return err?.message || "Too many failed authentication attempts. Try again later.";
  return `${label}: ${err?.message || err}`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function baseName(p) {
  const s = String(p || "");
  const parts = s.split(/[/\\\\]+/g).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : s;
}

function setAuthHint(text) {
  const el = document.getElementById("dbAuthHint");
  if (!el) return;
  el.textContent = text || "";
}

function setSqlHint(text, isWarn = false) {
  const el = document.getElementById("dbSqlHint");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isWarn ? "var(--warn)" : "var(--ink-soft)";
}

function setSelectedHint(text) {
  const el = document.getElementById("dbSelectedHint");
  if (!el) return;
  el.textContent = text || "";
}

function setActiveTab(name) {
  const tabs = {
    structure: document.getElementById("tabStructure"),
    data: document.getElementById("tabData"),
    sql: document.getElementById("tabSql"),
  };
  const panels = {
    structure: document.getElementById("panelStructure"),
    data: document.getElementById("panelData"),
    sql: document.getElementById("panelSql"),
  };

  Object.keys(tabs).forEach((k) => {
    const isActive = k === name;
    tabs[k]?.setAttribute("aria-selected", isActive ? "true" : "false");
    panels[k].hidden = !isActive;
  });
}

function renderTableHead(theadEl, cols) {
  if (!theadEl) return;
  theadEl.innerHTML = `<tr>${(cols || []).map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr>`;
}

function renderTableBody(tbodyEl, cols, rows) {
  if (!tbodyEl) return;
  const safeCols = Array.isArray(cols) ? cols : [];
  const safeRows = Array.isArray(rows) ? rows : [];

  tbodyEl.innerHTML = safeRows
    .map((r) => {
      const cells = safeCols.map((c) => {
        const v = r?.[c];
        if (v == null) return "<td class=\"admin-muted\">—</td>";
        const s = typeof v === "object" ? JSON.stringify(v) : String(v);
        return `<td><code>${escapeHtml(s)}</code></td>`;
      });
      return `<tr>${cells.join("")}</tr>`;
    })
    .join("");
}

const SQL_KEYWORDS = [
  "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "LIMIT", "OFFSET",
  "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "ON",
  "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
  "CREATE", "TABLE", "VIEW", "INDEX", "DROP", "ALTER",
  "WITH", "AS", "DISTINCT", "AND", "OR", "NOT", "NULL", "IS",
  "IN", "EXISTS", "LIKE", "GLOB", "BETWEEN",
  "CASE", "WHEN", "THEN", "ELSE", "END",
  "UNION", "ALL",
  "EXPLAIN", "QUERY", "PLAN", "PRAGMA",
];

let state = {
  tables: [],
  selectedTable: null,
  tableColumns: {}, // table -> [col]
  sqlSuggest: { open: false, items: [], active: 0, wordStart: 0, wordEnd: 0 },
  erCache: null,
};

function _mermaidReady() {
  return typeof window !== "undefined" && window.__mermaid && typeof window.__mermaid.render === "function";
}

function _mermaidIdFor(name) {
  return String(name || "").replace(/[^A-Za-z0-9_]/g, "_") || "t";
}

function _buildMermaidEr(schema) {
  const tables = schema?.tables || [];
  const idMap = {};
  tables.forEach((t) => {
    const name = String(t?.name || "");
    if (!name) return;
    let id = _mermaidIdFor(name);
    // avoid collisions
    let n = 2;
    while (Object.values(idMap).includes(id)) {
      id = `${id}_${n++}`;
    }
    idMap[name] = id;
  });

  const lines = ["erDiagram"];

  // Entities
  tables.forEach((t) => {
    const name = String(t?.name || "");
    const id = idMap[name];
    if (!id) return;
    lines.push(`  ${id} {`);
    const cols = t?.columns || [];
    cols.slice(0, 60).forEach((c) => {
      const colName = String(c?.name || "");
      if (!colName) return;
      const typ = String(c?.type || "TEXT").split(/\s+/)[0] || "TEXT";
      const flags = [];
      if (c?.pk) flags.push("PK");
      lines.push(`    ${typ} ${_mermaidIdFor(colName)}${flags.length ? " " + flags.join(" ") : ""}`);
    });
    if (cols.length > 60) lines.push("    TEXT _more");
    lines.push("  }");
  });

  // Relationships (parent ||--o{ child)
  tables.forEach((child) => {
    const childName = String(child?.name || "");
    const childId = idMap[childName];
    const fks = child?.foreignKeys || [];
    fks.forEach((fk) => {
      const parentName = String(fk?.table || "");
      const parentId = idMap[parentName];
      if (!parentId || !childId) return;
      const label = String(fk?.from || "");
      lines.push(`  ${parentId} ||--o{ ${childId} : "${label}"`);
    });
  });

  return { text: lines.join("\n"), idMap };
}

async function renderErDiagramIfNoSelection() {
  const erWrap = document.getElementById("dbErWrap");
  const structureWrap = document.getElementById("dbStructureWrap");
  const diagramEl = document.getElementById("dbErDiagram");
  if (!erWrap || !structureWrap || !diagramEl) return;

  if (state.selectedTable) {
    erWrap.hidden = true;
    return;
  }

  erWrap.hidden = false;
  structureWrap.hidden = true;
  // Keep the hint neutral while showing the ER diagram.
  setSelectedHint("Select a table on the left.");

  if (!_mermaidReady()) {
    diagramEl.innerHTML = `<p class="admin-muted">ER diagram renderer unavailable (Mermaid failed to load).</p>`;
    return;
  }

  try {
    const schema = state.erCache || (await fetchJson("/api/admin/db/er"));
    state.erCache = schema;
    if (!schema?.ok) {
      diagramEl.innerHTML = `<p class="admin-muted">Could not load ER schema.</p>`;
      return;
    }
    const built = _buildMermaidEr(schema);
    const id = `er_${Math.random().toString(16).slice(2)}`;
    const res = await window.__mermaid.render(id, built.text);
    diagramEl.innerHTML = res?.svg || `<pre class="admin-muted">${escapeHtml(built.text)}</pre>`;
  } catch (e) {
    diagramEl.innerHTML = `<p class="admin-muted">${escapeHtml(String(e?.message || e))}</p>`;
  }
}

async function ensureColumnsForTable(tableName) {
  const t = String(tableName || "");
  if (!t) return [];
  if (Array.isArray(state.tableColumns[t])) return state.tableColumns[t];
  try {
    const payload = await fetchJson(`/api/admin/db/table?name=${encodeURIComponent(t)}`);
    const cols = (payload?.columns || []).map((c) => String(c?.name || "")).filter(Boolean);
    state.tableColumns[t] = cols;
    return cols;
  } catch {
    state.tableColumns[t] = [];
    return [];
  }
}

function _sqlEditorEls() {
  return {
    ta: document.getElementById("dbSqlText"),
    suggest: document.getElementById("dbSqlSuggest"),
  };
}

function _caretPixelCoords(textarea, caretIndex) {
  // Mirror technique: render textarea content into an offscreen div with same styles,
  // then measure a marker span at the caret.
  const ta = textarea;
  const idx = Math.max(0, Math.min((ta?.value || "").length, caretIndex ?? (ta?.selectionStart ?? 0)));
  const cs = window.getComputedStyle(ta);

  const div = document.createElement("div");
  div.style.position = "absolute";
  div.style.visibility = "hidden";
  div.style.whiteSpace = "pre-wrap";
  div.style.wordWrap = "break-word";
  div.style.overflow = "hidden";

  // Copy relevant text metrics
  div.style.fontFamily = cs.fontFamily;
  div.style.fontSize = cs.fontSize;
  div.style.fontWeight = cs.fontWeight;
  div.style.letterSpacing = cs.letterSpacing;
  div.style.lineHeight = cs.lineHeight;
  div.style.padding = cs.padding;
  div.style.border = cs.border;
  div.style.boxSizing = cs.boxSizing;
  div.style.width = cs.width;

  // Match scroll position by shifting content
  div.style.height = cs.height;
  div.scrollTop = ta.scrollTop;
  div.scrollLeft = ta.scrollLeft;

  const text = ta.value || "";
  const before = text.slice(0, idx);
  const after = text.slice(idx);

  const span = document.createElement("span");
  span.textContent = after.length ? after[0] : ".";

  // Preserve newlines/spaces exactly
  div.textContent = before;
  const marker = document.createElement("span");
  marker.textContent = "\u200b";
  div.appendChild(marker);
  div.appendChild(span);

  document.body.appendChild(div);

  const taRect = ta.getBoundingClientRect();
  const markerRect = marker.getBoundingClientRect();
  const divRect = div.getBoundingClientRect();

  // caret position relative to textarea content box
  const x = markerRect.left - divRect.left;
  const y = markerRect.top - divRect.top;

  document.body.removeChild(div);

  return { x, y, taRect, lineHeight: Number.parseFloat(cs.lineHeight || "18") || 18 };
}

function positionSqlSuggest() {
  const { ta, suggest } = _sqlEditorEls();
  if (!ta || !suggest || suggest.hidden) return;
  const caret = ta.selectionStart ?? (ta.value || "").length;
  const coords = _caretPixelCoords(ta, caret);

  // Place popup just under caret line, inside the wrapper.
  // Wrapper is offsetParent for absolute positioning.
  const wrapper = ta.closest?.(".admin-db-sqlwrap");
  const wRect = wrapper?.getBoundingClientRect?.();
  if (!wrapper || !wRect) return;

  const top = coords.y + coords.lineHeight + 10; // below caret line
  const left = Math.max(10, Math.min(coords.x + 10, wrapper.clientWidth - 10));

  suggest.style.top = `${Math.round(top)}px`;
  suggest.style.left = `${Math.round(left)}px`;
  suggest.style.right = "auto";
}

function _tokenAtCursor(text, cursorIdx) {
  const i = Math.max(0, Math.min(text.length, cursorIdx));
  const isTok = (ch) => /[A-Za-z0-9_.]/.test(ch);
  let a = i;
  while (a > 0 && isTok(text[a - 1])) a -= 1;
  let b = i;
  while (b < text.length && isTok(text[b])) b += 1;
  const token = text.slice(a, b);
  return { token, start: a, end: b };
}

function _uniqueSorted(items) {
  const seen = new Set();
  const out = [];
  for (const it of items) {
    const k = `${it.kind}::${it.value}`.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(it);
  }
  out.sort((a, b) => a.value.localeCompare(b.value));
  return out;
}

function closeSqlSuggest() {
  const { suggest } = _sqlEditorEls();
  if (suggest) suggest.hidden = true;
  state.sqlSuggest.open = false;
  state.sqlSuggest.items = [];
  state.sqlSuggest.active = 0;
}

function _renderSqlSuggest() {
  const { suggest } = _sqlEditorEls();
  if (!suggest) return;
  const items = state.sqlSuggest.items || [];
  if (!items.length) {
    suggest.hidden = true;
    state.sqlSuggest.open = false;
    return;
  }
  suggest.hidden = false;
  state.sqlSuggest.open = true;
  const active = Math.max(0, Math.min(items.length - 1, state.sqlSuggest.active || 0));
  state.sqlSuggest.active = active;

  suggest.innerHTML = items
    .slice(0, 10)
    .map((it, idx) => {
      const cls = idx === active ? "admin-db-suggest-item admin-db-suggest-item--active" : "admin-db-suggest-item";
      return `<div class="${cls}" role="option" data-idx="${idx}">
        <span class="admin-db-suggest-k">${escapeHtml(it.value)}</span>
        <span class="admin-db-suggest-tag">${escapeHtml(it.kind)}</span>
      </div>`;
    })
    .join("");

  suggest.querySelectorAll?.(".admin-db-suggest-item")?.forEach((row) => {
    row.addEventListener("mousedown", (ev) => {
      // prevent textarea blur
      ev.preventDefault();
      const idx = Number(row.getAttribute("data-idx") || "0") || 0;
      state.sqlSuggest.active = idx;
      void acceptSqlSuggestion();
    });
  });

  positionSqlSuggest();
}

async function updateSqlSuggest() {
  const { ta } = _sqlEditorEls();
  if (!ta) return;
  const text = ta.value || "";
  const cursor = ta.selectionStart ?? text.length;
  const { token, start, end } = _tokenAtCursor(text, cursor);

  // Only open suggestions when user is in a token position.
  if (!token || !token.trim()) {
    closeSqlSuggest();
    return;
  }

  const raw = token;
  const parts = raw.split(".");
  const prefixRaw = parts[parts.length - 1] || "";
  const prefix = prefixRaw.toLowerCase();
  if (!prefix) {
    closeSqlSuggest();
    return;
  }

  let candidates = [];

  if (parts.length >= 2) {
    // table.column autocomplete
    const tableName = parts.slice(0, -1).join(".");
    const cols = await ensureColumnsForTable(tableName);
    candidates = cols.map((c) => ({ kind: "column", value: c, insert: c }));
  } else {
    const tables = (state.tables || []).map((t) => String(t?.name || "")).filter(Boolean);
    const tableItems = tables.map((t) => ({ kind: "table", value: t, insert: t }));
    const kwItems = SQL_KEYWORDS.map((k) => ({ kind: "keyword", value: k, insert: k }));

    const selectedCols = state.selectedTable ? await ensureColumnsForTable(state.selectedTable) : [];
    const colItems = selectedCols.map((c) => ({ kind: "column", value: c, insert: c }));

    candidates = kwItems.concat(tableItems).concat(colItems);
  }

  let matches = candidates.filter((c) => String(c.value || "").toLowerCase().startsWith(prefix));
  // If the user already completed the word (e.g. SELECT), don't keep suggesting the same token.
  matches = matches.filter((c) => {
    const v = String(c.value || "");
    return !(v.length === prefixRaw.length && v.toLowerCase() === prefix);
  });
  matches = _uniqueSorted(matches).slice(0, 10);

  if (!matches.length) {
    closeSqlSuggest();
    return;
  }

  state.sqlSuggest.items = matches;
  state.sqlSuggest.active = 0;
  state.sqlSuggest.wordStart = start + (raw.length - prefixRaw.length);
  state.sqlSuggest.wordEnd = end;
  _renderSqlSuggest();
}

async function acceptSqlSuggestion() {
  const { ta } = _sqlEditorEls();
  if (!ta) return;
  if (!state.sqlSuggest.open || !(state.sqlSuggest.items || []).length) return;
  const idx = Math.max(0, Math.min(state.sqlSuggest.items.length - 1, state.sqlSuggest.active || 0));
  const it = state.sqlSuggest.items[idx];
  if (!it) return;

  const text = ta.value || "";
  const a = state.sqlSuggest.wordStart ?? ta.selectionStart ?? 0;
  const b = state.sqlSuggest.wordEnd ?? ta.selectionEnd ?? a;
  const insert = String(it.insert || it.value || "");

  ta.value = text.slice(0, a) + insert + text.slice(b);
  const next = a + insert.length;
  ta.setSelectionRange(next, next);
  closeSqlSuggest();
  await updateSqlSuggest();
}

async function loadOverview() {
  const payload = await fetchJson("/api/admin/db/overview");
  const fileEl = document.getElementById("dbFilePath");
  if (fileEl) {
    const full = String(payload?.dbPath || "—");
    const short = full === "—" ? "—" : baseName(full);
    fileEl.title = full;
    fileEl.innerHTML = `<code>${escapeHtml(short)}</code>`;
  }

  const list = document.getElementById("dbDatabaseList");
  if (list) {
    const dbs = payload?.databases || [];
    list.innerHTML = dbs
      .map((d) => {
        const name = String(d?.name || "");
        const file = String(d?.file || "");
        return `<button type="button" class="admin-db-attach-row" data-db="${escapeHtml(name)}" title="${escapeHtml(file)}">
          <code class="admin-db-attach-name">${escapeHtml(name)}</code>
          <span class="admin-db-attach-arrow admin-muted">→</span>
          <code class="admin-db-attach-file">${escapeHtml(baseName(file) || file)}</code>
        </button>`;
      })
      .join("");
    if (!dbs.length) list.innerHTML = `<div class="admin-muted">(none)</div>`;

    // Clicking an attached DB row returns to the ER diagram view.
    list.querySelectorAll?.("button.admin-db-attach-row")?.forEach((btn) => {
      btn.addEventListener("click", () => {
        state.selectedTable = null;
        setActiveTab("structure");
        void loadTables().then(() => renderErDiagramIfNoSelection());
      });
    });
  }
}

async function loadTables() {
  const payload = await fetchJson("/api/admin/db/tables");
  state.tables = payload?.tables || [];
  const el = document.getElementById("dbTablesList");
  if (!el) return;

  el.innerHTML = state.tables
    .map((t) => {
      const name = String(t?.name || "");
      const type = String(t?.type || "table");
      const selected = state.selectedTable === name;
      const cls = selected ? "admin-db-list-item admin-db-list-item--active" : "admin-db-list-item";
      return `<button type="button" class="${cls}" data-table="${escapeHtml(name)}" role="listitem">
        <span><code>${escapeHtml(name)}</code></span>
        <span class="admin-muted">${escapeHtml(type)}</span>
      </button>`;
    })
    .join("");

  el.querySelectorAll?.("button[data-table]")?.forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.getAttribute("data-table");
      if (!name) return;
      void selectTable(name);
    });
  });
}

async function selectTable(name) {
  state.selectedTable = String(name);
  await loadTables();

  setSelectedHint(`Selected: ${state.selectedTable}`);
  const erWrap = document.getElementById("dbErWrap");
  if (erWrap) erWrap.hidden = true;
  const wrap = document.getElementById("dbStructureWrap");
  if (wrap) wrap.hidden = false;

  const payload = await fetchJson(`/api/admin/db/table?name=${encodeURIComponent(state.selectedTable)}`);

  const colsBody = document.getElementById("dbColumnsBody");
  const indexesBody = document.getElementById("dbIndexesBody");

  const cols = payload?.columns || [];
  state.tableColumns[state.selectedTable] = cols.map((c) => String(c?.name || "")).filter(Boolean);
  if (colsBody) {
    colsBody.innerHTML = cols
      .map(
        (c) => `<tr>
        <td><code>${escapeHtml(c?.name)}</code></td>
        <td><code>${escapeHtml(c?.type || "")}</code></td>
        <td>${c?.notnull ? "Yes" : "No"}</td>
        <td><code>${escapeHtml(c?.dflt_value ?? "")}</code></td>
        <td>${c?.pk ? "Yes" : "No"}</td>
      </tr>`,
      )
      .join("");
    if (!cols.length) colsBody.innerHTML = `<tr><td class="admin-muted" colspan="5">(no columns)</td></tr>`;
  }

  const idx = payload?.indexes || [];
  if (indexesBody) {
    indexesBody.innerHTML = idx
      .map(
        (i) => `<tr>
        <td><code>${escapeHtml(i?.name)}</code></td>
        <td>${i?.unique ? "Yes" : "No"}</td>
        <td><code>${escapeHtml(i?.origin || "")}</code></td>
        <td>${i?.partial ? "Yes" : "No"}</td>
        <td><code>${escapeHtml((i?.columns || []).join(", "))}</code></td>
      </tr>`,
      )
      .join("");
    if (!idx.length) indexesBody.innerHTML = `<tr><td class="admin-muted" colspan="5">(no indexes)</td></tr>`;
  }

  // Data preview
  const preview = await fetchJson(`/api/admin/db/preview?name=${encodeURIComponent(state.selectedTable)}&limit=100`);
  renderTableHead(document.getElementById("dbDataHead"), preview?.columns || []);
  renderTableBody(document.getElementById("dbDataBody"), preview?.columns || [], preview?.rows || []);
}

async function runSql() {
  const sqlEl = document.getElementById("dbSqlText");
  const limitEl = document.getElementById("dbRowLimit");
  const sql = (sqlEl?.value || "").trim();
  const limit = Math.max(1, Math.min(5000, Number(limitEl?.value || 200) || 200));

  if (!sql) {
    setSqlHint("Enter a SQL statement first.", true);
    return;
  }

  setSqlHint("Running…");
  const payload = await fetchJson("/api/admin/db/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sql, limit }),
  });

  if (!payload?.ok) {
    setSqlHint(payload?.error || "Query failed.", true);
    return;
  }

  setSqlHint(
    payload?.meta?.rowCount != null
      ? `OK · ${payload.meta.rowCount} row(s) · ${payload.meta.elapsedMs} ms`
      : `OK · ${payload?.meta?.elapsedMs} ms`,
  );

  const wrap = document.getElementById("dbSqlResultsWrap");
  if (wrap) wrap.hidden = false;
  renderTableHead(document.getElementById("dbSqlHead"), payload?.columns || []);
  renderTableBody(document.getElementById("dbSqlBody"), payload?.columns || [], payload?.rows || []);
}

function setupTabs() {
  const tabs = [
    ["tabStructure", "structure"],
    ["tabData", "data"],
    ["tabSql", "sql"],
  ];
  tabs.forEach(([id, name]) => {
    const btn = document.getElementById(id);
    btn?.addEventListener("click", () => setActiveTab(name));
  });
}

async function refreshAll() {
  setAuthHint("");
  try {
    await loadOverview();
    await loadTables();
    if (state.selectedTable) {
      await selectTable(state.selectedTable);
    } else {
      await renderErDiagramIfNoSelection();
    }
  } catch (e) {
    setAuthHint(adminEndpointErrorMessage(e, "DB"));
  }
}

function main() {
  setupTabs();
  setActiveTab("structure");

  document.getElementById("dbRefreshBtn")?.addEventListener("click", () => void refreshAll());
  document.getElementById("dbRunSqlBtn")?.addEventListener("click", () => void runSql());
  const sqlTa = document.getElementById("dbSqlText");
  sqlTa?.addEventListener("input", () => void updateSqlSuggest());
  sqlTa?.addEventListener("scroll", () => positionSqlSuggest());
  window.addEventListener("resize", () => positionSqlSuggest());
  sqlTa?.addEventListener("blur", () => {
    // Small delay so clicking a suggestion doesn't immediately close.
    window.setTimeout(closeSqlSuggest, 120);
  });
  sqlTa?.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
      ev.preventDefault();
      void runSql();
      return;
    }
    if (ev.key === "Enter" && state.sqlSuggest.open && !ev.shiftKey) {
      ev.preventDefault();
      void acceptSqlSuggestion();
      return;
    }
    if (ev.key === "Escape") {
      closeSqlSuggest();
      return;
    }
    if (state.sqlSuggest.open && (ev.key === "ArrowDown" || ev.key === "ArrowUp")) {
      ev.preventDefault();
      const n = state.sqlSuggest.items.length;
      const d = ev.key === "ArrowDown" ? 1 : -1;
      state.sqlSuggest.active = (state.sqlSuggest.active + d + n) % n;
      _renderSqlSuggest();
      return;
    }
    if (ev.key === "Tab" && state.sqlSuggest.open) {
      ev.preventDefault();
      void acceptSqlSuggestion();
      return;
    }
  });

  setSelectedHint("Select a table on the left.");
  void refreshAll();
}

main();

