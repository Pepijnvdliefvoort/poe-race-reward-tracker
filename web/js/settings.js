import { dom } from "./state.js";

/**
 * Manages settings modal display, loading, and persisting configuration.
 */

let isSettingsOpen = false;
let settingsState = null;

/**
 * Initialize settings modal and attach event handlers.
 */
export async function initSettingsModal() {
  const overlay = document.getElementById("settingsOverlay");
  const openBtn = document.getElementById("settingsBtn");
  const closeBtn = document.getElementById("settingsCloseBtn");
  const saveBtn = document.getElementById("settingsSaveBtn");
  const saveStatus = document.getElementById("settingsSaveStatus");

  const cfgEnabled = document.getElementById("cfgEnabled");
  const cfgThreshold = document.getElementById("cfgThreshold");
  const cfgHistoryCycles = document.getElementById("cfgHistoryCycles");
  const cfgMinTotalResults = document.getElementById("cfgMinTotalResults");
  const cfgMinFloorListings = document.getElementById("cfgMinFloorListings");
  const cfgFloorBandPct = document.getElementById("cfgFloorBandPct");
  const cfgLowLiquidityExtraDropPct = document.getElementById("cfgLowLiquidityExtraDropPct");
  const cfgCooldownCycles = document.getElementById("cfgCooldownCycles");

  settingsState = {
    overlay,
    cfgEnabled,
    cfgThreshold,
    cfgHistoryCycles,
    cfgMinTotalResults,
    cfgMinFloorListings,
    cfgFloorBandPct,
    cfgLowLiquidityExtraDropPct,
    cfgCooldownCycles,
    saveStatus,
  };

  // Attach event listeners
  openBtn.addEventListener("click", openModal);
  closeBtn.addEventListener("click", closeModal);
  saveBtn.addEventListener("click", saveSettings);

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isSettingsOpen) closeModal();
  });
}

function openModal() {
  isSettingsOpen = true;
  settingsState.overlay.classList.add("open");
  settingsState.overlay.removeAttribute("aria-hidden");
  document.body.classList.add("modal-open");
  settingsState.saveStatus.textContent = "";
  loadSettings();
}

function closeModal() {
  isSettingsOpen = false;
  settingsState.overlay.classList.remove("open");
  settingsState.overlay.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

async function loadSettings() {
  try {
    const resp = await fetch("/api/config", { cache: "no-store" });
    if (!resp.ok) return;
    const cfg = await resp.json();
    settingsState.cfgEnabled.checked = Boolean(cfg.alert_enabled);
    settingsState.cfgThreshold.value = cfg.alert_threshold_pct ?? 30;
    settingsState.cfgHistoryCycles.value = cfg.alert_history_cycles ?? 10;
    settingsState.cfgMinTotalResults.value = cfg.alert_min_total_results ?? 8;
    settingsState.cfgMinFloorListings.value = cfg.alert_min_floor_listings ?? 2;
    settingsState.cfgFloorBandPct.value = cfg.alert_floor_band_pct ?? 6;
    settingsState.cfgLowLiquidityExtraDropPct.value =
      cfg.alert_low_liquidity_extra_drop_pct ?? 22;
    settingsState.cfgCooldownCycles.value = cfg.alert_cooldown_cycles ?? 6;
  } catch {
    // silently ignore; defaults remain
  }
}

async function saveSettings() {
  const payload = {
    alert_enabled: settingsState.cfgEnabled.checked,
    alert_threshold_pct: Number(settingsState.cfgThreshold.value) || 30,
    alert_history_cycles: Number(settingsState.cfgHistoryCycles.value) || 10,
    alert_min_total_results: Number(settingsState.cfgMinTotalResults.value) || 8,
    alert_min_floor_listings: Number(settingsState.cfgMinFloorListings.value) || 2,
    alert_floor_band_pct: Number(settingsState.cfgFloorBandPct.value) || 6,
    alert_low_liquidity_extra_drop_pct:
      Number(settingsState.cfgLowLiquidityExtraDropPct.value) || 22,
    alert_cooldown_cycles: Number(settingsState.cfgCooldownCycles.value) || 6,
  };
  try {
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (resp.ok) {
      settingsState.saveStatus.textContent = "Saved.";
      settingsState.saveStatus.style.color = "var(--ok)";
      closeModal();
    } else {
      settingsState.saveStatus.textContent = "Save failed.";
      settingsState.saveStatus.style.color = "var(--error)";
    }
  } catch {
    settingsState.saveStatus.textContent = "Save failed.";
    settingsState.saveStatus.style.color = "var(--error)";
  }
}
