const form = document.getElementById("companionForm");
const widget = document.getElementById("companionWidget");
const panel = document.getElementById("companionPanel");
const toggleBtn = document.getElementById("companionToggle");
const closeBtn = document.getElementById("companionClose");
const resizeBtn = document.getElementById("companionResize");
const headerEl = panel?.querySelector(".companion-header");
const wealthInput = document.getElementById("companionWealth");
const currencySelect = document.getElementById("companionCurrency");
const riskSelect = document.getElementById("companionRisk");
const modeSelect = document.getElementById("companionMode");
const submitBtn = document.getElementById("companionSubmit");
const statusEl = document.getElementById("companionStatus");
const resultsEl = document.getElementById("companionResults");

const PREFERENCES_STORAGE_KEY = "companion.preferences.v1";

const CATEGORY_HELP = {
  "Best fit": "High overall score for your wealth and risk profile.",
  Liquid: "Stronger inferred sale activity, meaning there is clearer evidence of demand.",
  Speculative: "Higher-risk pick because of thin supply, large trend movement, or speculative settings.",
  "Value watch": "Recent price is down enough that it may be worth monitoring.",
  Watchlist: "Decent candidate, but without a stronger specific signal.",
};

const RISK_HELP = {
  safe: "Safe risk favors steadier picks with stronger liquidity and avoids concentrating too much wealth in one item.",
  balanced: "Balanced risk mixes liquidity, price fit, and trend so the suggestions are not too conservative or too speculative.",
  speculative: "Speculative risk gives more weight to trend movement and upside, so it can include thinner or more volatile markets.",
};

function formatMirror(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "n/a";
  return `${n.toLocaleString(undefined, { maximumFractionDigits: 2 })} mirror${n === 1 ? "" : "s"}`;
}

function formatPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "n/a";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}%`;
}

function formatMirrorDelta(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "n/a";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toLocaleString(undefined, { maximumFractionDigits: 2 })} mirror${Math.abs(n) === 1 ? "" : "s"}`;
}

function setStatus(message, tone = "") {
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.dataset.tone = tone;
}

function setLoading(isLoading) {
  if (submitBtn) {
    submitBtn.disabled = isLoading;
    submitBtn.textContent = isLoading ? "Estimating..." : "Estimate";
  }
}

function selectHasValue(select, value) {
  return Array.from(select.options).some((option) => option.value === value);
}

function readStoredPreferences() {
  try {
    const raw = window.localStorage.getItem(PREFERENCES_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storePreferences() {
  if (!wealthInput || !currencySelect || !riskSelect || !modeSelect) return;

  try {
    window.localStorage.setItem(
      PREFERENCES_STORAGE_KEY,
      JSON.stringify({
        wealth: wealthInput.value,
        currency: currencySelect.value,
        risk: riskSelect.value,
        mode: modeSelect.value,
      }),
    );
  } catch {
    // Storage may be disabled; preferences are a convenience only.
  }
}

function restorePreferences() {
  if (!wealthInput || !currencySelect || !riskSelect || !modeSelect) return;

  const preferences = readStoredPreferences();
  if (!preferences || typeof preferences !== "object") return;

  if (typeof preferences.wealth === "string" && preferences.wealth.trim() !== "") {
    wealthInput.value = preferences.wealth;
  }
  if (typeof preferences.currency === "string" && selectHasValue(currencySelect, preferences.currency)) {
    currencySelect.value = preferences.currency;
  }
  if (typeof preferences.risk === "string" && selectHasValue(riskSelect, preferences.risk)) {
    riskSelect.value = preferences.risk;
  }
  if (typeof preferences.mode === "string" && selectHasValue(modeSelect, preferences.mode)) {
    modeSelect.value = preferences.mode;
  }
}

function initPreferenceStorage() {
  restorePreferences();
  wealthInput?.addEventListener("input", storePreferences);
  currencySelect?.addEventListener("change", storePreferences);
  riskSelect?.addEventListener("change", storePreferences);
  modeSelect?.addEventListener("change", storePreferences);
}

function openCompanion() {
  if (!widget || !panel || !toggleBtn) return;
  widget.classList.remove("companion-widget-collapsed");
  panel.hidden = false;
  toggleBtn.setAttribute("aria-expanded", "true");
  window.requestAnimationFrame(() => wealthInput?.focus());
}

function closeCompanion() {
  if (!widget || !panel || !toggleBtn) return;
  widget.classList.add("companion-widget-collapsed");
  panel.hidden = true;
  toggleBtn.setAttribute("aria-expanded", "false");
  toggleBtn.focus();
}

function toggleCompanion() {
  if (!panel || panel.hidden) {
    openCompanion();
  } else {
    closeCompanion();
  }
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function initCompanionResize() {
  if (!panel || !resizeBtn) return;

  const minWidth = 320;
  const minHeight = 360;
  const viewportMargin = 28;

  resizeBtn.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    const startRect = panel.getBoundingClientRect();
    const startX = event.clientX;
    const startY = event.clientY;

    resizeBtn.setPointerCapture?.(event.pointerId);
    document.body.classList.add("companion-resizing");

    const onPointerMove = (moveEvent) => {
      const maxWidth = Math.max(minWidth, window.innerWidth - viewportMargin);
      const maxHeight = Math.max(minHeight, window.innerHeight - viewportMargin);
      const nextWidth = clamp(startRect.width - (moveEvent.clientX - startX), minWidth, maxWidth);
      const nextHeight = clamp(startRect.height - (moveEvent.clientY - startY), minHeight, maxHeight);
      panel.style.width = `${Math.round(nextWidth)}px`;
      panel.style.height = `${Math.round(nextHeight)}px`;
      panel.style.maxHeight = `${Math.round(maxHeight)}px`;
    };

    const onPointerUp = () => {
      document.body.classList.remove("companion-resizing");
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
  });

  resizeBtn.addEventListener("dblclick", () => {
    panel.style.width = "";
    panel.style.height = "";
    panel.style.maxHeight = "";
  });
}

async function companionAuthenticated() {
  try {
    const response = await fetch("/api/companion/auth", { cache: "no-store" });
    if (!response.ok) {
      return false;
    }
    const payload = await response.json();
    return Boolean(payload.authenticated);
  } catch {
    return false;
  }
}

function createMetric(label, value) {
  const metric = document.createElement("span");
  metric.className = "companion-metric";

  const metricLabel = document.createElement("span");
  metricLabel.className = "companion-metric-label";
  metricLabel.textContent = label;

  const metricValue = document.createElement("strong");
  metricValue.textContent = value;

  metric.append(metricLabel, metricValue);
  return metric;
}

function createLabelHelp() {
  const details = document.createElement("details");
  details.className = "companion-label-help";

  const summary = document.createElement("summary");
  summary.textContent = "What do the labels mean?";

  const list = document.createElement("dl");
  for (const [label, description] of Object.entries(CATEGORY_HELP)) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = description;
    list.append(term, detail);
  }

  details.append(summary, list);
  return details;
}

function createOutlookBlock(title, lines, tone = "") {
  const block = document.createElement("div");
  block.className = tone ? `companion-outlook companion-outlook-${tone}` : "companion-outlook";

  const heading = document.createElement("strong");
  heading.className = "companion-outlook-title";
  heading.textContent = title;

  const text = document.createElement("p");
  text.textContent = lines.filter(Boolean).join(" ");

  block.append(heading, text);
  return block;
}

function renderRecommendation(rec, options = {}) {
  const { portfolio = false } = options;
  const card = document.createElement("article");
  card.className = portfolio ? "companion-card companion-card-portfolio" : "companion-card";

  const media = document.createElement("div");
  media.className = "companion-card-media";
  if (rec.imagePath) {
    const img = document.createElement("img");
    img.src = rec.imagePath;
    img.alt = `${rec.itemName} art`;
    img.loading = "lazy";
    media.appendChild(img);
  }

  const body = document.createElement("div");
  body.className = "companion-card-body";

  const headingRow = document.createElement("div");
  headingRow.className = "companion-card-heading";

  const title = document.createElement("h3");
  title.textContent = rec.itemName || "Unknown item";

  const badge = document.createElement("span");
  badge.className = "companion-badge";
  badge.textContent = rec.category || "Watchlist";
  badge.title = CATEGORY_HELP[badge.textContent] || CATEGORY_HELP.Watchlist;

  headingRow.append(title, badge);

  const metrics = document.createElement("div");
  metrics.className = "companion-metrics";
  if (portfolio) {
    metrics.append(
      createMetric("Units", `${rec.portfolioUnits ?? rec.suggestedUnits ?? 1}`),
      createMetric("Position", formatMirror(rec.portfolioAllocationMirror)),
      createMetric("Share", rec.portfolioShare == null ? "n/a" : `${Math.round(Number(rec.portfolioShare) * 100)}%`),
      createMetric("Score", `${rec.score ?? 0}`),
      createMetric("Entry", formatMirror(rec.priceMirror)),
    );
  } else {
    metrics.append(
      createMetric("Score", `${rec.score ?? 0}`),
      createMetric("Entry", formatMirror(rec.priceMirror)),
      createMetric("Allocation", formatMirror(rec.suggestedAllocationMirror)),
      createMetric("30d trend", rec.trendPct30d == null ? "n/a" : formatPercent(rec.trendPct30d)),
      createMetric("Est. sold", `~${rec.inferredSales30d ?? 0}`),
    );
  }

  const reasons = document.createElement("ul");
  reasons.className = "companion-reasons";
  for (const reason of rec.reasons || []) {
    const item = document.createElement("li");
    item.textContent = reason;
    reasons.appendChild(item);
  }

  body.append(headingRow, metrics, reasons);

  if (rec.flip) {
    const flip = rec.flip;
    const flipLines = flip.viable
      ? [
          `Buy at ${formatMirror(flip.buyPriceMirror)}, then relist for ${formatMirror(flip.relistPriceMirror)}.`,
          `That is about ${formatMirrorDelta(flip.expectedProfitMirror)} gross profit (${formatPercent(flip.expectedProfitPct)}).`,
          flip.sellCondition,
        ]
      : [
          flip.reason || "No clean immediate flip gap was found in the latest whole-mirror ladder.",
          flip.nextMarketPriceMirror ? `Next listing: ${formatMirror(flip.nextMarketPriceMirror)}.` : "",
        ];
    body.appendChild(createOutlookBlock(flip.viable ? "Immediate flip" : "No clean flip", flipLines, flip.viable ? "good" : "muted"));
  }

  if (rec.hold30d) {
    const hold = rec.hold30d;
    body.appendChild(
      createOutlookBlock(
        "30-day hold",
        [
          `Expected price: ${formatMirror(hold.expectedPriceMirror)}.`,
          `Expected return: ${formatMirrorDelta(hold.expectedProfitMirror)} (${formatPercent(hold.expectedReturnPct)}).`,
          hold.sellTiming,
          hold.cycleNote,
        ],
        Number(hold.expectedProfitMirror) > 0 ? "good" : "muted",
      ),
    );
  }

  if (portfolio && rec.portfolioReason) {
    const portfolioReason = document.createElement("p");
    portfolioReason.className = "companion-portfolio-reason";
    portfolioReason.textContent = rec.portfolioReason;
    body.appendChild(portfolioReason);
  }

  if (Array.isArray(rec.warnings) && rec.warnings.length) {
    const warnings = document.createElement("p");
    warnings.className = "companion-warning";
    warnings.textContent = rec.warnings.join(" ");
    body.appendChild(warnings);
  }

  card.append(media, body);
  return card;
}

function renderPortfolio(payload) {
  if (!resultsEl) return false;
  const portfolio = payload.portfolio;
  const positions = Array.isArray(portfolio?.positions) ? portfolio.positions : [];
  if (!portfolio || !positions.length) {
    return false;
  }

  const summary = document.createElement("div");
  summary.className = "companion-summary companion-message companion-message-bot";
  const noteText = Array.isArray(portfolio.notes) && portfolio.notes.length ? ` ${portfolio.notes.join(" ")}` : "";
  summary.textContent = `I built a portfolio plan for ${formatMirror(payload.wealthMirror)}. It deploys ${formatMirror(portfolio.deployedMirror)} across ${positions.length} position${positions.length === 1 ? "" : "s"} and keeps ${formatMirror(portfolio.cashReserveMirror)} liquid. The ranking favors inferred demand, trend, and real whole-mirror ladder gaps; listing count is only risk context. ${RISK_HELP[payload.risk] || RISK_HELP.balanced}${noteText}`;
  resultsEl.appendChild(summary);
  resultsEl.appendChild(createLabelHelp());

  for (const rec of positions) {
    resultsEl.appendChild(renderRecommendation(rec, { portfolio: true }));
  }
  return true;
}

function renderResults(payload) {
  if (!resultsEl) return;
  resultsEl.replaceChildren();

  const recommendations = Array.isArray(payload.recommendations) ? payload.recommendations : [];
  if (payload.mode === "portfolio" && renderPortfolio(payload)) {
    resultsEl.hidden = false;
    return;
  }

  if (!recommendations.length) {
    const empty = document.createElement("div");
    empty.className = "companion-empty";
    empty.textContent = "No affordable opportunities matched the current data. Try a larger wealth value or a different risk profile.";
    resultsEl.appendChild(empty);
    resultsEl.hidden = false;
    return;
  }

  const summary = document.createElement("div");
  summary.className = "companion-summary companion-message companion-message-bot";
  summary.textContent = `I found ${recommendations.length} ranked estimate${recommendations.length === 1 ? "" : "s"} for a ${formatMirror(payload.wealthMirror)} budget. The ranking now favors inferred sale activity, price trend, and real whole-mirror ladder gaps; raw listing count is used as risk context, not as a reason to buy. ${RISK_HELP[payload.risk] || RISK_HELP.balanced}`;
  resultsEl.appendChild(summary);
  resultsEl.appendChild(createLabelHelp());

  for (const rec of recommendations) {
    resultsEl.appendChild(renderRecommendation(rec));
  }
  resultsEl.hidden = false;
}

async function submitCompanion(event) {
  event.preventDefault();
  if (!wealthInput || !currencySelect || !riskSelect || !modeSelect) return;

  const wealth = Number(wealthInput.value);
  if (!Number.isFinite(wealth) || wealth <= 0) {
    setStatus("Enter a positive wealth value.", "error");
    wealthInput.focus();
    return;
  }
  storePreferences();

  setLoading(true);
  setStatus("Checking market data...", "");

  try {
    const response = await fetch("/api/companion/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({
        wealth,
        currency: currencySelect.value,
        risk: riskSelect.value,
        mode: modeSelect.value,
      }),
    });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    renderResults(payload);
    setStatus(payload.disclaimer || "Results are estimates from inferred market data.", "ok");
  } catch (error) {
    if (resultsEl) {
      resultsEl.hidden = true;
      resultsEl.replaceChildren();
    }
    setStatus(`Companion error: ${error.message}`, "error");
  } finally {
    setLoading(false);
  }
}

export function initCompanion() {
  if (!form || !widget) return;
  initPreferenceStorage();
  form.addEventListener("submit", submitCompanion);
  toggleBtn?.addEventListener("click", toggleCompanion);
  closeBtn?.addEventListener("click", closeCompanion);
  headerEl?.addEventListener("click", closeCompanion);
  initCompanionResize();
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && panel && !panel.hidden) {
      closeCompanion();
    }
  });
  companionAuthenticated().then((authenticated) => {
    if (!authenticated) return;
    widget.hidden = false;
    document.body.classList.add("companion-available");
  });
}
