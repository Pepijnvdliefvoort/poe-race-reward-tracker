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
const threadEl = panel?.querySelector(".companion-thread");

const PREFERENCES_STORAGE_KEY = "companion.preferences.v1";
const TYPING_MIN_MS = 240;
const TYPING_MAX_MS = 620;
const FIRST_MESSAGE_TYPING_MS = 250;
const MESSAGE_PAUSE_MS = 250;

let renderSequenceId = 0;

const CATEGORY_HELP = {
  "Best fit": "High overall score for your wealth and risk profile.",
  Liquid: "Stronger inferred sale activity, meaning there is clearer evidence of demand.",
  Speculative: "Higher-risk pick because of thin supply, large trend movement, or speculative settings.",
  "Value watch": "Recent price is down enough that it may be worth monitoring.",
  Watchlist: "Decent candidate, but without a stronger specific signal.",
};

const RISK_POLICY = {
  safe: { deploy: 0.6, position: 0.22 },
  balanced: { deploy: 0.75, position: 0.3 },
  speculative: { deploy: 0.85, position: 0.4 },
};

const RISK_HELP = {
  safe: "Safe mode leans toward proven demand, cleaner entries, and tighter position sizing. Hybrid ML only nudges names that clear the confidence gate.",
  balanced: "Balanced mode blends demand, price fit, trend, and whole-mirror ladder structure, then lets hybrid ML nudge medium- and strong-confidence names instead of fully overriding the ranking.",
  speculative: "Speculative mode gives more room to trend and upside, but still treats thin listings and weak sales support as risk rather than proof of value.",
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

function wait(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function scrollThreadToBottom() {
  if (!threadEl) return;
  if (threadEl.scrollHeight > threadEl.clientHeight) return;
  threadEl.scrollTop = threadEl.scrollHeight;
}

function createBotMessage(text, extraClass = "") {
  const message = document.createElement("div");
  message.className = ["companion-message", "companion-message-bot", extraClass].filter(Boolean).join(" ");
  message.textContent = text;
  return message;
}

function createUserMessage(text) {
  const message = document.createElement("div");
  message.className = "companion-message companion-message-user";
  message.textContent = text;
  return message;
}

function formatCompanionChoice(value) {
  return String(value || "")
    .trim()
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function summarizeUserRequest({ wealth, currency, risk, mode }) {
  const wealthSummary = `${wealthInput?.value || wealth} ${formatCompanionChoice(currency)}`;
  return `I have ${wealthSummary}, want a ${String(risk || "balanced").toLowerCase()} risk setup, and want ${formatCompanionChoice(mode)}.`;
}

function startConversationTurn(summary) {
  if (!resultsEl) return;
  resultsEl.replaceChildren(createUserMessage(summary));
  resultsEl.hidden = false;
  scrollThreadToBottom();
}

function createTypingMessage() {
  const message = document.createElement("div");
  message.className = "companion-message companion-message-bot companion-message-typing";
  message.setAttribute("aria-hidden", "true");

  for (let i = 0; i < 3; i += 1) {
    const dot = document.createElement("span");
    dot.className = "companion-typing-dot";
    message.appendChild(dot);
  }
  return message;
}

function typingDelayForMessage(text, messageIndex = 0) {
  const rawText = String(text || "");
  const punctuationCount = (rawText.match(/[,:;]/g) || []).length;
  const charDelay = Math.max(0, Math.min(rawText.length * 2, 130));
  const punctuationDelay = Math.min(punctuationCount * 18, 54);
  const jitter = Math.floor(Math.random() * 70);
  const baseDelay = messageIndex === 0 ? FIRST_MESSAGE_TYPING_MS : TYPING_MIN_MS;
  return Math.min(TYPING_MAX_MS, baseDelay + charDelay + punctuationDelay + jitter);
}

async function appendBotMessageWithTyping(text, sequenceId, extraClass = "", messageIndex = 0) {
  if (!resultsEl) return false;

  const typingMessage = createTypingMessage();
  resultsEl.appendChild(typingMessage);
  scrollThreadToBottom();

  await wait(typingDelayForMessage(text, messageIndex));
  if (sequenceId !== renderSequenceId) {
    typingMessage.remove();
    return false;
  }

  typingMessage.replaceWith(createBotMessage(text, extraClass));
  scrollThreadToBottom();
  await wait(MESSAGE_PAUSE_MS);
  return sequenceId === renderSequenceId;
}

async function appendBotTopicMessages(messages, sequenceId) {
  for (const [messageIndex, message] of messages.entries()) {
    if (!message) continue;
    const appended = await appendBotMessageWithTyping(message, sequenceId, "", messageIndex);
    if (!appended) return false;
  }
  return sequenceId === renderSequenceId;
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

function formatPercentWhole(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "n/a";
  return `${Math.round(n)}%`;
}

function buildHybridSummary(payload) {
  const mlShadow = payload?.mlShadow || {};
  const telemetry = payload?.mlTelemetry || {};
  const alpha = Number(mlShadow.alphaHeuristic);
  const heuristicWeight = Number.isFinite(alpha) ? Math.round(alpha * 100) : null;
  const mlWeight = Number.isFinite(alpha) ? Math.round((1 - alpha) * 100) : null;
  const minTier = String(mlShadow.minConfidenceTier || "medium");
  const total = Number(telemetry.totalCandidates ?? 0);
  const applied = Number(telemetry.hybridAppliedCandidates ?? 0);
  const skippedBelowConfidence = Number(telemetry.hybridSkippedReasonCounts?.["below-min-confidence"] ?? 0);

  if (!mlShadow.enabled) {
    return "ML is off, so ranking is heuristic-only.";
  }

  if (!mlShadow.hybridEnabled) {
    return "ML is shadow-only; ranking still comes from the heuristic stack.";
  }

  if (heuristicWeight != null && mlWeight != null) {
    return `Hybrid ranking: ${heuristicWeight}% heuristic, ${mlWeight}% ML for ${formatCompanionChoice(minTier)}+ confidence.`;
  }
  if (total > 0 && skippedBelowConfidence > 0) {
    return `Hybrid ranking applied to ${applied}/${total}; ${skippedBelowConfidence} stayed heuristic-only below ${formatCompanionChoice(minTier)} confidence.`;
  }
  return `Hybrid ranking is on with a ${formatCompanionChoice(minTier)} confidence gate.`;
}

function buildRankingValueSummary(payload) {
  const risk = String(payload?.risk || "balanced").toLowerCase();
  if (risk === "safe") {
    return "Safe mode prioritizes demand and clean entries first.";
  }
  if (risk === "speculative") {
    return "Speculative mode gives more weight to trend and upside.";
  }
  return "Balanced mode weighs demand, price fit, trend, and real ladder structure.";
}

function buildPortfolioDeploymentSummary(payload) {
  const portfolio = payload?.portfolio || {};
  const positions = Array.isArray(portfolio.positions) ? portfolio.positions : [];
  const risk = String(payload?.risk || "balanced").toLowerCase();
  const policy = RISK_POLICY[risk] || RISK_POLICY.balanced;
  const wealthMirror = Number(payload?.wealthMirror ?? 0);
  const targetDeploy = Number(portfolio.targetDeployedMirror ?? wealthMirror * policy.deploy);
  const deployedMirror = Number(portfolio.deployedMirror ?? 0);
  const cashReserveMirror = Number(portfolio.cashReserveMirror ?? 0);

  let message = `I built a portfolio plan for ${formatMirror(wealthMirror)}. It deploys ${formatMirror(deployedMirror)} across ${positions.length} position${positions.length === 1 ? "" : "s"} and keeps ${formatMirror(cashReserveMirror)} liquid.`;

  if (Number.isFinite(targetDeploy) && deployedMirror < targetDeploy) {
    message += ` Target was ${formatMirror(targetDeploy)}, but more names did not clear the filters.`;
  }

  return message;
}

function buildPortfolioNotes(payload) {
  const portfolio = payload?.portfolio || {};
  const risk = String(payload?.risk || "balanced").toLowerCase();
  const policy = RISK_POLICY[risk] || RISK_POLICY.balanced;
  const notes = [
    `${formatCompanionChoice(risk)} targets about ${formatPercentWhole(policy.deploy * 100)} deployed with positions capped near ${formatPercentWhole(policy.position * 100)}.`,
  ];

  const targetDeploy = Number(portfolio.targetDeployedMirror ?? 0);
  const deployedMirror = Number(portfolio.deployedMirror ?? 0);
  if (Number.isFinite(targetDeploy) && Number.isFinite(deployedMirror) && deployedMirror < targetDeploy) {
    notes.push("Unused cash stays liquid because the remaining names were weaker, thinner, or too large.");
  }

  return notes;
}

function createFactList(items) {
  const facts = document.createElement("dl");
  facts.className = "companion-facts";

  for (const [label, value] of items) {
    if (!value) continue;
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    facts.append(term, detail);
  }

  return facts;
}

function summarizeWhyPicked(rec) {
  const parts = [];
  const sales = Number(rec.inferredSales30d ?? 0);
  const trend = Number(rec.trendPct30d);
  const entry = Number(rec.priceMirror);
  const wealthShare = Number(rec.wealthShare);
  const expectedValue = Number(rec.expectedValue30d);

  if (rec.rankingSource === "hybrid" && Number.isFinite(expectedValue)) {
    parts.push(
      expectedValue > 0
        ? `ML favors the 30-day value at ${formatMirrorDelta(expectedValue)}.`
        : `ML still ranked it highly even with ${formatMirrorDelta(expectedValue)} expected 30-day value.`,
    );
  } else if (rec.flip?.viable) {
    parts.push(`Picked for the immediate ladder gap of ${formatMirrorDelta(rec.flip.expectedProfitMirror)}.`);
  } else if (sales > 0) {
    parts.push(`Picked for active demand with about ${sales} inferred sale${sales === 1 ? "" : "s"} in 30 days.`);
  } else {
    parts.push("Picked as a higher-risk setup rather than a proven liquid market.");
  }

  if (Number.isFinite(trend)) {
    if (trend >= 15) {
      parts.push(`Momentum is strong at ${formatPercent(trend)}.`);
    } else if (trend <= -15) {
      parts.push(`Price is off ${formatPercent(trend)}, so this reads more like a rebound setup.`);
    } else {
      parts.push(`Recent pricing is stable at ${formatPercent(trend)}.`);
    }
  }

  if (Number.isFinite(entry) && Number.isFinite(wealthShare)) {
    parts.push(`Entry is ${formatMirror(entry)} using ${Math.round(wealthShare * 100)}% of bankroll.`);
  }

  return parts.join(" ");
}

function buildFlipFacts(flip) {
  if (!flip) return [];
  if (flip.viable) {
    return [
      ["Buy", formatMirror(flip.buyPriceMirror)],
      ["Relist", formatMirror(flip.relistPriceMirror)],
      ["Gross edge", `${formatMirrorDelta(flip.expectedProfitMirror)} (${formatPercent(flip.expectedProfitPct)})`],
      ["Setup", flip.sellCondition || flip.reason],
    ];
  }
  return [
    ["Status", flip.reason || "No clean immediate flip gap in the latest ladder."],
    ["Next listing", flip.nextMarketPriceMirror ? formatMirror(flip.nextMarketPriceMirror) : "n/a"],
  ];
}

function buildHoldFacts(hold) {
  if (!hold) return [];
  return [
    ["Target", formatMirror(hold.expectedPriceMirror)],
    ["30d return", `${formatMirrorDelta(hold.expectedProfitMirror)} (${formatPercent(hold.expectedReturnPct)})`],
    ["Plan", hold.sellTiming],
  ];
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

  const summary = document.createElement("p");
  summary.className = "companion-pick-summary";
  summary.textContent = summarizeWhyPicked(rec);

  body.append(headingRow, metrics, summary);

  if (rec.flip) {
    const flip = rec.flip;
    const block = document.createElement("div");
    block.className = flip.viable ? "companion-outlook companion-outlook-good" : "companion-outlook companion-outlook-muted";

    const heading = document.createElement("strong");
    heading.className = "companion-outlook-title";
    heading.textContent = flip.viable ? "Immediate flip" : "Flip setup";

    block.append(heading, createFactList(buildFlipFacts(flip)));
    body.appendChild(block);
  }

  if (rec.hold30d) {
    const hold = rec.hold30d;
    const block = document.createElement("div");
    block.className = Number(hold.expectedProfitMirror) > 0 ? "companion-outlook companion-outlook-good" : "companion-outlook companion-outlook-muted";

    const heading = document.createElement("strong");
    heading.className = "companion-outlook-title";
    heading.textContent = "30-day hold";

    block.append(heading, createFactList(buildHoldFacts(hold)));

    if (hold.cycleNote) {
      const note = document.createElement("p");
      note.className = "companion-outlook-note";
      note.textContent = hold.cycleNote;
      block.appendChild(note);
    }

    body.appendChild(block);
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

async function renderPortfolio(payload, sequenceId) {
  if (!resultsEl) return false;
  const portfolio = payload.portfolio;
  const positions = Array.isArray(portfolio?.positions) ? portfolio.positions : [];
  if (!portfolio || !positions.length) {
    return false;
  }

  const topics = [
    buildPortfolioDeploymentSummary(payload),
    buildHybridSummary(payload),
    buildRankingValueSummary(payload),
    RISK_HELP[payload.risk] || RISK_HELP.balanced,
  ];
  const portfolioNotes = buildPortfolioNotes(payload);
  if (portfolioNotes.length) {
    topics.push(portfolioNotes.join(" "));
  }

  const renderedTopics = await appendBotTopicMessages(topics, sequenceId);
  if (!renderedTopics) {
    return false;
  }

  resultsEl.appendChild(createLabelHelp());

  for (const rec of positions) {
    resultsEl.appendChild(renderRecommendation(rec, { portfolio: true }));
  }
  scrollThreadToBottom();
  return true;
}

async function renderResults(payload, sequenceId) {
  if (!resultsEl) return;

  const recommendations = Array.isArray(payload.recommendations) ? payload.recommendations : [];
  if (payload.mode === "portfolio" && (await renderPortfolio(payload, sequenceId))) {
    resultsEl.hidden = false;
    return;
  }

  if (!recommendations.length) {
    await appendBotMessageWithTyping(
      "No affordable opportunities matched the current data. Try a larger wealth value or a different risk profile.",
      sequenceId,
      "companion-empty",
    );
    resultsEl.hidden = false;
    return;
  }

  const topics = [
    `I found ${recommendations.length} ranked estimate${recommendations.length === 1 ? "" : "s"} for a ${formatMirror(payload.wealthMirror)} budget.`,
    buildHybridSummary(payload),
    buildRankingValueSummary(payload),
    RISK_HELP[payload.risk] || RISK_HELP.balanced,
  ];
  const renderedTopics = await appendBotTopicMessages(topics, sequenceId);
  if (!renderedTopics) {
    return;
  }

  resultsEl.appendChild(createLabelHelp());

  for (const rec of recommendations) {
    resultsEl.appendChild(renderRecommendation(rec));
  }
  resultsEl.hidden = false;
  scrollThreadToBottom();
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

  const requestSummary = summarizeUserRequest({
    wealth,
    currency: currencySelect.value,
    risk: riskSelect.value,
    mode: modeSelect.value,
  });
  startConversationTurn(requestSummary);

  setLoading(true);
  setStatus("Checking market data...", "");
  const sequenceId = ++renderSequenceId;

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
    if (sequenceId !== renderSequenceId) return;
    await renderResults(payload, sequenceId);
    if (sequenceId !== renderSequenceId) return;
    setStatus(payload.disclaimer || "Results are estimates from inferred market data.", "ok");
  } catch (error) {
    if (sequenceId !== renderSequenceId) return;
    if (resultsEl) {
      resultsEl.hidden = true;
      resultsEl.replaceChildren();
    }
    setStatus(`Companion error: ${error.message}`, "error");
  } finally {
    if (sequenceId === renderSequenceId) {
      setLoading(false);
    }
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
