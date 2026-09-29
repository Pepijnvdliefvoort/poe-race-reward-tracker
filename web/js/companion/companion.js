const widget = document.getElementById("companionWidget");
const panel = document.getElementById("companionPanel");
const toggleBtn = document.getElementById("companionToggle");
const closeBtn = document.getElementById("companionClose");
const resizeHandle = document.getElementById("companionResize");
const form = document.getElementById("companionForm");
const wealthInput = document.getElementById("companionWealth");
const currencySelect = document.getElementById("companionCurrency");
const submitBtn = document.getElementById("companionSubmit");
const statusEl = document.getElementById("companionStatus");
const resultsEl = document.getElementById("companionResults");
const riskHelpEl = document.getElementById("companionRiskHelp");

const PREFERENCES_STORAGE_KEY = "companion.preferences.v1";
const WIDTH_STORAGE_KEY = "companion.width.v1";
const MIN_WIDTH = 360;
const MAX_WIDTH = 760;

let requestSequence = 0;
let hasSearched = false;

const CATEGORY_HELP = {
  "Quick flip": "Expected to resell within about two weeks at the target price.",
  Steady: "Expected to resell within about 15 to 45 days.",
  "Slow hold": "Profitable on paper, but expected to take more than 45 days to sell.",
  Speculative: "Very few recent sales, so the sell-time estimate leans on market-wide averages.",
};

const RISK_HELP = {
  safe: "Only items with at least two recent sales and a 50%+ chance of selling within the horizon.",
  balanced: "Items with at least a 25% chance of selling within the horizon.",
  speculative: "Every item with a positive expected return, including ones with almost no sales history.",
};

// ---- small helpers -------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function formatMirror(value, { unit = true } = {}) {
  const n = num(value);
  if (n == null) return "n/a";
  const digits = n >= 10 ? 1 : 2;
  const text = n.toLocaleString(undefined, { maximumFractionDigits: digits });
  return unit ? `${text} mirror${n === 1 ? "" : "s"}` : text;
}

function formatPercent(value, digits = 1) {
  const n = num(value);
  if (n == null) return "n/a";
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

function formatDays(value) {
  const n = num(value);
  if (n == null) return "n/a";
  return n < 1.5 ? "~1 day" : `~${Math.round(n)} days`;
}

function checkedValue(name, fallback) {
  const input = form?.querySelector(`input[name="${name}"]:checked`);
  return input ? input.value : fallback;
}

function setChecked(name, value) {
  const input = form?.querySelector(`input[name="${name}"][value="${CSS.escape(String(value))}"]`);
  if (input) input.checked = true;
}

function tradeSearchUrl(rec) {
  if (!rec.queryId) return null;
  const league = encodeURIComponent(rec.league || "Standard");
  return `https://www.pathofexile.com/trade/search/${league}/${encodeURIComponent(rec.queryId)}`;
}

function setStatus(message, tone = "") {
  if (!statusEl) return;
  statusEl.textContent = message || "";
  statusEl.dataset.tone = tone;
}

function setLoading(isLoading) {
  if (!submitBtn) return;
  submitBtn.disabled = isLoading;
  submitBtn.textContent = isLoading ? "Finding picks..." : "Find picks";
}

function updateRiskHelp() {
  if (riskHelpEl) riskHelpEl.textContent = RISK_HELP[checkedValue("companionRisk", "balanced")] || "";
}

// ---- preferences ---------------------------------------------------------------------------

function readJson(key) {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function writeJson(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage can be unavailable; preferences are a convenience only.
  }
}

function storePreferences() {
  writeJson(PREFERENCES_STORAGE_KEY, {
    wealth: wealthInput?.value ?? "",
    currency: currencySelect?.value ?? "mirror",
    risk: checkedValue("companionRisk", "balanced"),
    mode: checkedValue("companionMode", "ranked"),
  });
}

function restorePreferences() {
  const prefs = readJson(PREFERENCES_STORAGE_KEY);
  if (!prefs || typeof prefs !== "object") return;
  if (typeof prefs.wealth === "string" && prefs.wealth.trim() && wealthInput) wealthInput.value = prefs.wealth;
  if (typeof prefs.currency === "string" && currencySelect) {
    if (Array.from(currencySelect.options).some((o) => o.value === prefs.currency)) currencySelect.value = prefs.currency;
  }
  if (typeof prefs.risk === "string") setChecked("companionRisk", prefs.risk);
  if (typeof prefs.mode === "string") setChecked("companionMode", prefs.mode);
}

// ---- drawer open / close / resize ------------------------------------------------------------

function applyWidth(width) {
  const max = Math.min(MAX_WIDTH, window.innerWidth - 40);
  const clamped = Math.round(Math.max(MIN_WIDTH, Math.min(max, width)));
  widget?.style.setProperty("--cp-width", `${clamped}px`);
  return clamped;
}

function openCompanion() {
  if (!widget || !panel || !toggleBtn) return;
  panel.hidden = false;
  widget.classList.add("is-open");
  toggleBtn.setAttribute("aria-expanded", "true");
  window.requestAnimationFrame(() => wealthInput?.focus());
}

function closeCompanion() {
  if (!widget || !panel || !toggleBtn) return;
  panel.hidden = true;
  widget.classList.remove("is-open");
  toggleBtn.setAttribute("aria-expanded", "false");
  toggleBtn.focus();
}

function initResize() {
  if (!resizeHandle || !widget) return;
  const saved = num(readJson(WIDTH_STORAGE_KEY));
  if (saved) applyWidth(saved);

  resizeHandle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    resizeHandle.setPointerCapture?.(event.pointerId);
    document.body.classList.add("companion-resizing");
    let width = null;
    const onMove = (moveEvent) => {
      width = applyWidth(window.innerWidth - moveEvent.clientX);
    };
    const onUp = () => {
      document.body.classList.remove("companion-resizing");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (width) writeJson(WIDTH_STORAGE_KEY, width);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });

  resizeHandle.addEventListener("dblclick", () => {
    widget.style.removeProperty("--cp-width");
    try {
      window.localStorage.removeItem(WIDTH_STORAGE_KEY);
    } catch {
      // ignore
    }
  });
}

// ---- rendering -------------------------------------------------------------------------------

function renderSkeleton() {
  const wrap = el("div", "companion-results");
  for (let i = 0; i < 4; i += 1) wrap.appendChild(el("div", "companion-skeleton"));
  resultsEl?.replaceChildren(...wrap.childNodes);
}

function renderMessage(title, text) {
  const box = el("div", "companion-empty");
  box.append(el("p", "companion-empty-title", title), el("p", "", text));
  resultsEl?.replaceChildren(box);
}

function tile(label, value, title = "") {
  const node = el("div", "companion-tile");
  if (title) node.title = title;
  node.append(el("span", "companion-tile-label", label), el("span", "companion-tile-value", value));
  return node;
}

function legend() {
  const details = el("details", "companion-legend");
  details.appendChild(el("summary", "", "Labels"));
  const list = el("dl");
  for (const [label, text] of Object.entries(CATEGORY_HELP)) {
    const dt = el("dt");
    dt.appendChild(tagEl(label));
    list.append(dt, el("dd", "", text));
  }
  details.appendChild(list);
  return details;
}

function methodText(payload) {
  const ranking = payload.ranking || {};
  const horizon = num(ranking.horizonDays);
  const span = horizon ? `${Math.round(horizon)}-day` : "horizon";
  const who = ranking.modelEnabled ? "a trained model that beat the formula on past data" : "the transparent formula";
  return `Ranked by ${who}: expected gain divided by expected days to sell, with a ${span} window.`;
}

function renderSummary(payload, picks) {
  const section = el("section", "companion-summary");
  const tiles = el("div", "companion-tiles");
  const isPortfolio = payload.mode === "portfolio" && payload.portfolio;

  if (isPortfolio) {
    const plan = payload.portfolio;
    const wealth = num(payload.wealthMirror) || 0;
    const deployed = num(plan.deployedMirror) || 0;
    tiles.append(
      tile("Deployed", formatMirror(deployed, { unit: false }), `${formatMirror(deployed)} of ${formatMirror(wealth)}`),
      tile("Cash left", formatMirror(plan.cashReserveMirror, { unit: false })),
      tile("Positions", String(picks.length)),
    );
    section.appendChild(tiles);

    const deploy = el("div", "companion-deploy");
    const bar = el("div", "companion-deploy-bar");
    const fill = el("span");
    fill.style.width = `${wealth > 0 ? Math.min(100, (deployed / wealth) * 100) : 0}%`;
    bar.appendChild(fill);
    const labels = el("div", "companion-deploy-labels");
    labels.append(
      el("span", "", `${Math.round((num(plan.deploymentPct) || 0) * 100)}% deployed`),
      el("span", "", `target ${formatMirror(plan.targetDeployedMirror, { unit: false })}`),
    );
    deploy.append(bar, labels);
    section.appendChild(deploy);
  } else {
    const best = picks.length ? num(picks[0].estimate?.returnPerDayPct) : null;
    tiles.append(
      tile("Picks", String(picks.length)),
      tile("Best / day", best == null ? "n/a" : formatPercent(best, 2)),
      tile("Budget", formatMirror(payload.wealthMirror, { unit: false }), formatMirror(payload.wealthMirror)),
    );
    section.appendChild(tiles);
  }

  const method = el("div", "companion-method");
  method.append(el("p", "", methodText(payload)), legend());
  section.appendChild(method);
  return section;
}

function tagEl(kind, text = kind) {
  const tag = el("span", "companion-tag", text);
  tag.dataset.kind = kind;
  if (CATEGORY_HELP[kind]) tag.title = CATEGORY_HELP[kind];
  return tag;
}

function meterEl(probability) {
  const p = num(probability);
  const pct = p == null ? 0 : Math.round(p * 100);
  const meter = el("div", "companion-meter");
  meter.dataset.level = pct >= 70 ? "high" : pct < 40 ? "low" : "mid";
  const track = el("div", "companion-meter-track");
  track.setAttribute("role", "img");
  track.setAttribute("aria-label", `${pct}% chance to sell`);
  const fill = el("span");
  fill.style.width = `${pct}%`;
  track.appendChild(fill);
  meter.append(track, el("span", "", `${pct}% sells`));
  return meter;
}

function factsEl(items) {
  const dl = el("dl", "companion-facts");
  for (const [label, value, isText = false] of items) {
    if (!value) continue;
    const row = el("div");
    row.append(el("dt", "", label), el("dd", isText ? "is-text" : "", value));
    dl.appendChild(row);
  }
  return dl;
}

function callout(tone, title, text) {
  const box = el("div", `companion-callout companion-callout-${tone}`);
  box.append(el("strong", "", title), el("span", "", text));
  return box;
}

function renderPick(rec, rank, { portfolio = false } = {}) {
  const est = rec.estimate || {};
  const item = el("li");
  const details = el("details", "companion-pick");
  const summary = el("summary");

  const img = el("div", "companion-pick-img");
  if (rec.imagePath) {
    const image = el("img");
    image.src = rec.imagePath;
    image.alt = "";
    image.loading = "lazy";
    img.appendChild(image);
  }

  const name = el("div", "companion-pick-name");
  name.append(el("strong", "", rec.itemName || "Unknown item"), tagEl(rec.category || "Speculative"));
  if (est.plan === "one_mirror") name.appendChild(tagEl("plan", "1 mirror"));

  const rpdValue = num(est.returnPerDayPct);
  const rpd = el("div", "companion-rpd", formatPercent(rpdValue, 2));
  rpd.dataset.sign = rpdValue == null ? "" : rpdValue >= 0 ? "up" : "down";
  rpd.appendChild(el("small", "", "per day"));

  const line = el("div", "companion-pick-line");
  const priceSpan = el("span");
  priceSpan.append(el("b", "", formatMirror(rec.priceMirror, { unit: false })), document.createTextNode(" → "),
    el("b", "", formatMirror(est.askPriceMirror, { unit: false })));
  line.append(priceSpan, el("span", "", formatPercent(est.returnIfSoldPct)), el("span", "", formatDays(est.expectedDays)));
  if (portfolio) {
    line.appendChild(el("span", "", `×${rec.portfolioUnits ?? 1} = ${formatMirror(rec.portfolioAllocationMirror, { unit: false })}`));
  }

  summary.append(el("span", "companion-rank", String(rank)), img, name, rpd, line, meterEl(est.sellProbability));
  details.appendChild(summary);

  const body = el("div", "companion-details");
  const planLabel = est.plan === "one_mirror" ? "Exactly 1 mirror" : "Undercut recent sales";
  body.appendChild(
    factsEl([
      ["Plan", planLabel, true],
      ["Fair value", formatMirror(est.fairValueMirror)],
      ["If it sells", formatPercent(est.returnIfSoldPct)],
      ["Expected", `${formatPercent(est.expectedReturnPct)} · ${formatDays(est.expectedDays)}`],
      ["Queue ahead", est.queueAhead != null ? `${est.queueAhead} listing${est.queueAhead === 1 ? "" : "s"}` : null],
      [
        portfolio ? "Position" : "Suggested",
        portfolio
          ? `${rec.portfolioUnits ?? 1} × ${formatMirror(rec.priceMirror)} (${Math.round((num(rec.portfolioShare) || 0) * 100)}%)`
          : `${rec.suggestedUnits ?? 1} unit${rec.suggestedUnits === 1 ? "" : "s"} (max ${rec.maxUnits ?? 1})`,
      ],
    ]),
  );

  if (Array.isArray(rec.reasons) && rec.reasons.length) {
    const list = el("ul", "companion-reasons");
    for (const reason of rec.reasons) list.appendChild(el("li", "", reason));
    body.appendChild(list);
  }
  if (rec.flip?.viable) {
    body.appendChild(
      callout("good", "Immediate ladder gap", `${rec.flip.sellCondition || rec.flip.reason} Gross ${formatPercent(rec.flip.expectedProfitPct)}.`),
    );
  }
  if (Array.isArray(rec.warnings) && rec.warnings.length) {
    body.appendChild(callout("warn", "Heads up", rec.warnings.join(" ")));
  }
  const url = tradeSearchUrl(rec);
  if (url) {
    const link = el("a", "companion-trade-link", "Open trade search ↗");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    body.appendChild(link);
  }

  details.appendChild(body);
  item.appendChild(details);
  return item;
}

function renderResults(payload) {
  const isPortfolio = payload.mode === "portfolio";
  const picks = isPortfolio ? payload.portfolio?.positions || [] : payload.recommendations || [];
  if (!picks.length) {
    const skipped = payload.skipped || {};
    const hint = skipped.unaffordable
      ? " Several items are above your budget; try a larger budget."
      : skipped.risk_filtered
        ? " Some items were filtered by the risk setting; try Speculative."
        : "";
    renderMessage("No picks right now", `Nothing affordable has a positive expected return at the moment.${hint}`);
    return;
  }

  const list = el("ol", "companion-picks");
  picks.forEach((rec, index) => list.appendChild(renderPick(rec, index + 1, { portfolio: isPortfolio })));
  resultsEl?.replaceChildren(renderSummary(payload, picks), list);
}

// ---- requests --------------------------------------------------------------------------------

async function runSearch() {
  const wealth = Number(wealthInput?.value);
  if (!Number.isFinite(wealth) || wealth <= 0) {
    setStatus("Enter a budget above zero.", "error");
    wealthInput?.focus();
    return;
  }
  storePreferences();
  hasSearched = true;

  const sequence = ++requestSequence;
  setLoading(true);
  setStatus("");
  renderSkeleton();

  try {
    const response = await fetch("/api/companion/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({
        wealth,
        currency: currencySelect?.value || "mirror",
        risk: checkedValue("companionRisk", "balanced"),
        mode: checkedValue("companionMode", "ranked"),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    if (sequence !== requestSequence) return;
    renderResults(payload);
  } catch (error) {
    if (sequence !== requestSequence) return;
    renderMessage("Could not load picks", String(error?.message || error));
    setStatus("The companion request failed.", "error");
  } finally {
    if (sequence === requestSequence) setLoading(false);
  }
}

async function companionAuthenticated() {
  try {
    const response = await fetch("/api/companion/auth", { cache: "no-store" });
    if (!response.ok) return false;
    const payload = await response.json();
    return Boolean(payload.authenticated);
  } catch {
    return false;
  }
}

export function initCompanion() {
  if (!form || !widget || !panel) return;
  restorePreferences();
  updateRiskHelp();
  initResize();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch();
  });
  wealthInput?.addEventListener("input", storePreferences);
  currencySelect?.addEventListener("change", storePreferences);
  form.querySelectorAll('input[type="radio"]').forEach((input) =>
    input.addEventListener("change", () => {
      updateRiskHelp();
      storePreferences();
      if (hasSearched) runSearch(); // re-run with the new risk / view once results are on screen
    }),
  );

  toggleBtn?.addEventListener("click", () => (panel.hidden ? openCompanion() : closeCompanion()));
  closeBtn?.addEventListener("click", closeCompanion);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) closeCompanion();
  });

  companionAuthenticated().then((authenticated) => {
    if (!authenticated) return;
    widget.hidden = false;
    document.body.classList.add("companion-available");
  });
}
