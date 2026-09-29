/**
 * Small macOS-style UI helpers for the admin apps:
 * - confirmSheet(): a confirmation sheet that slides down from the window's top edge
 *   (replaces window.confirm); destructive actions can require typing a word
 * - toast(): a short notification in the top-right corner
 * - formatWhen(): readable timestamps ("Today 14:05 · 3h ago"), exact time for tooltips
 */

let sheetOpen = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * @param {object} opts
 * @param {string} opts.title
 * @param {string} [opts.message]  plain text; blank lines separate paragraphs
 * @param {string} [opts.confirmLabel]
 * @param {boolean} [opts.destructive]  red confirm button + warning icon
 * @param {string} [opts.requireText]  the user must type this exact text to enable confirm
 * @returns {Promise<boolean>}
 */
export function confirmSheet({ title, message = "", confirmLabel = "Continue", destructive = false, requireText = "" } = {}) {
  if (sheetOpen) return Promise.resolve(false);
  const host = document.querySelector(".mac-app") || document.body;
  const previousFocus = document.activeElement;

  return new Promise((resolve) => {
    const backdrop = el("div", "mac-sheet-backdrop");
    const sheet = el("div", `mac-sheet${destructive ? " mac-sheet--destructive" : ""}`);
    sheet.setAttribute("role", "alertdialog");
    sheet.setAttribute("aria-modal", "true");
    const titleId = `mac-sheet-title-${Date.now()}`;
    sheet.setAttribute("aria-labelledby", titleId);

    const icon = el("div", "mac-sheet-icon", destructive ? "!" : "?");
    icon.setAttribute("aria-hidden", "true");
    const heading = el("h2", "mac-sheet-title", title);
    heading.id = titleId;
    sheet.append(icon, heading);
    for (const para of String(message).split(/\n\s*\n/)) {
      if (para.trim()) sheet.appendChild(el("p", "mac-sheet-text", para.trim()));
    }

    let input = null;
    if (requireText) {
      const label = el("label", "mac-sheet-require");
      label.append(document.createTextNode("Type "), el("strong", "", requireText), document.createTextNode(" to confirm"));
      input = el("input", "mac-sheet-input");
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      label.appendChild(input);
      sheet.appendChild(label);
    }

    const actions = el("div", "mac-sheet-actions");
    const cancelBtn = el("button", "admin-btn admin-btn-ghost", "Cancel");
    cancelBtn.type = "button";
    const okBtn = el("button", `admin-btn${destructive ? " admin-btn-destructive" : ""}`, confirmLabel);
    okBtn.type = "button";
    actions.append(cancelBtn, okBtn);
    sheet.appendChild(actions);

    const syncEnabled = () => {
      okBtn.disabled = !!requireText && input.value.trim() !== requireText;
    };
    syncEnabled();
    input?.addEventListener("input", syncEnabled);

    const finish = (result) => {
      document.removeEventListener("keydown", onKey, true);
      sheet.classList.remove("mac-sheet--shown");
      backdrop.classList.remove("mac-sheet-backdrop--shown");
      window.setTimeout(() => {
        backdrop.remove();
        sheetOpen = null;
        previousFocus?.focus?.({ preventScroll: true });
      }, 180);
      resolve(result);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        finish(false);
      } else if (ev.key === "Enter" && !okBtn.disabled && (ev.target === input || !input)) {
        ev.preventDefault();
        finish(true);
      } else if (ev.key === "Tab") {
        // Keep focus inside the sheet.
        const focusables = [input, cancelBtn, okBtn].filter((n) => n && !n.disabled);
        const i = focusables.indexOf(document.activeElement);
        const next = ev.shiftKey ? i - 1 : i + 1;
        ev.preventDefault();
        focusables[(next + focusables.length) % focusables.length]?.focus();
      }
    };
    cancelBtn.addEventListener("click", () => finish(false));
    okBtn.addEventListener("click", () => finish(true));
    backdrop.addEventListener("click", (ev) => {
      if (ev.target === backdrop) finish(false);
    });
    document.addEventListener("keydown", onKey, true);

    backdrop.appendChild(sheet);
    host.appendChild(backdrop);
    sheetOpen = sheet;
    void sheet.offsetWidth; // start the slide-in from the collapsed state
    backdrop.classList.add("mac-sheet-backdrop--shown");
    sheet.classList.add("mac-sheet--shown");
    (input || (destructive ? cancelBtn : okBtn)).focus({ preventScroll: true });
  });
}

let toastHost = null;

/**
 * @param {string} message
 * @param {{ kind?: "success" | "error" | "info", duration?: number }} [opts]
 */
export function toast(message, { kind = "success", duration = 4000 } = {}) {
  if (!message) return;
  if (!toastHost || !toastHost.isConnected) {
    toastHost = el("div", "mac-toasts");
    toastHost.setAttribute("aria-live", "polite");
    document.body.appendChild(toastHost);
  }
  const item = el("div", `mac-toast mac-toast--${kind}`);
  item.setAttribute("role", kind === "error" ? "alert" : "status");
  const icon = el("span", "mac-toast-icon", kind === "error" ? "!" : kind === "info" ? "i" : "✓");
  icon.setAttribute("aria-hidden", "true");
  const text = el("span", "mac-toast-text", message);
  const close = el("button", "mac-toast-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Dismiss notification");
  item.append(icon, text, close);
  toastHost.appendChild(item);
  void item.offsetWidth;
  item.classList.add("mac-toast--shown");

  const dismiss = () => {
    item.classList.remove("mac-toast--shown");
    window.setTimeout(() => item.remove(), 200);
  };
  close.addEventListener("click", dismiss);
  // Errors stay a little longer; hovering keeps any toast open.
  let timer = window.setTimeout(dismiss, kind === "error" ? duration * 2 : duration);
  item.addEventListener("mouseenter", () => window.clearTimeout(timer));
  item.addEventListener("mouseleave", () => {
    timer = window.setTimeout(dismiss, 1500);
  });
}

function parseWhen(value) {
  if (value == null || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") return new Date(value);
  const raw = String(value).trim();
  // Timestamps without a zone are UTC (browsers would read them as local time).
  const d = new Date(/([zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : `${raw}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

function relative(ms) {
  const abs = Math.abs(ms);
  const future = ms < 0;
  const units = [
    [86400000, "d"],
    [3600000, "h"],
    [60000, "m"],
  ];
  for (const [size, unit] of units) {
    if (abs >= size) {
      const n = Math.floor(abs / size);
      return future ? `in ${n}${unit}` : `${n}${unit} ago`;
    }
  }
  return "just now";
}

/**
 * @returns {{ text: string, title: string }} text like "Today 14:05 · 3h ago", title = full local time
 */
export function formatWhen(value) {
  const d = parseWhen(value);
  if (!d) return { text: value ? String(value) : "—", title: "" };
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  let day;
  if (dayDiff === 0) day = "Today";
  else if (dayDiff === 1) day = "Yesterday";
  else if (dayDiff === -1) day = "Tomorrow";
  else
    day = d.toLocaleDateString([], {
      day: "numeric",
      month: "short",
      ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
    });
  return {
    text: `${day} ${time} · ${relative(now.getTime() - d.getTime())}`,
    title: d.toLocaleString([], { dateStyle: "full", timeStyle: "medium" }),
  };
}
