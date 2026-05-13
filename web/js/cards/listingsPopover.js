import { REFRESH_MS } from "../core/state.js";

const listingsPreviewCache = new Map();
const listingsPreviewInFlight = new Map();
const LISTINGS_PREVIEW_CACHE_TTL_MS = Math.max(REFRESH_MS * 2, 8000);
const LISTINGS_POPOVER_CLOSE_DELAY_MS = 180;
const MOBILE_MEDIA_QUERY = "(hover: none) and (pointer: coarse), (max-width: 1024px)";

let globalListingsOverlay = null;
let activeListingsOverlayClose = null;
let globalListingsOverlayOpenedAt = 0;

function currencyIconPath(currencyRaw) {
  const c = (currencyRaw || "").toString().trim().toLowerCase();
  if (!c) return null;

  // Trade API currencies vary a bit in naming; normalize common variants.
  if (c === "mirror" || c === "mirrors" || c.includes("mirror of kalandra")) {
    return "/assets/MirrorofKalandra.png";
  }
  if (c === "divine" || c === "divines" || c === "div" || c.includes("divine orb")) {
    return "/assets/DivineOrb.png";
  }
  if (c === "exalted" || c === "exa" || c.includes("exalted orb")) {
    return "/assets/ExaltedOrb.png";
  }
  if (c === "ancient-reliquary-key" || c === "ancient reliquary key" || c.includes("reliquary key")) {
    return "/assets/AncientReliquaryKey.png";
  }
  return null;
}

function formatCurrencyLabel(currencyRaw) {
  const c = (currencyRaw || "").toString().trim().toLowerCase();
  if (!c) return "";
  if (c === "ancient-reliquary-key") return "ancient reliquary key";
  return c;
}

function clearNodeChildren(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function isMobileViewport() {
  return window.matchMedia?.(MOBILE_MEDIA_QUERY)?.matches ?? window.innerWidth <= 1024;
}

function ensureGlobalListingsOverlay() {
  if (globalListingsOverlay) {
    return globalListingsOverlay;
  }

  const overlay = document.createElement("div");
  overlay.className = "listings-popover-overlay";
  overlay.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    // If the overlay was just opened via the same press/tap, the "click" from release
    // can land on the overlay and immediately close it. Ignore that first click.
    if (globalListingsOverlayOpenedAt && performance.now() - globalListingsOverlayOpenedAt < 350) {
      return;
    }
    activeListingsOverlayClose?.();
  });

  // Overlay is page-level; the listings popover is also portaled to <body> on mobile.
  // This makes z-index ordering deterministic: card < overlay < popover.
  document.body.appendChild(overlay);
  globalListingsOverlay = overlay;
  return overlay;
}

function openGlobalListingsOverlay(onClose) {
  if (!isMobileViewport()) return;
  ensureGlobalListingsOverlay().classList.add("open");
  globalListingsOverlayOpenedAt = performance.now();
  activeListingsOverlayClose = onClose;
}

function closeGlobalListingsOverlay(onClose) {
  if (!globalListingsOverlay) return;
  if (activeListingsOverlayClose === onClose) {
    activeListingsOverlayClose = null;
    globalListingsOverlay.classList.remove("open");
  }
}

function mountListingsPopoverToBody(entry) {
  if (!isMobileViewport()) return;
  if (entry.listingsPopoverMountedToBody) return;
  if (!entry.listingsPopoverHome || !entry.listingsPopoverPlaceholder) return;

  entry.listingsPopoverMountedToBody = true;
  // Swap the in-card popover with a placeholder so layout doesn't shift.
  entry.listingsPopoverHome.replaceChild(entry.listingsPopoverPlaceholder, entry.listingsPopover);
  document.body.appendChild(entry.listingsPopover);

  entry.listingsPopover.classList.add("listings-popover--global");
  entry.listingsPopover.style.position = "absolute";
  entry.listingsPopover.style.left = "0px";
  entry.listingsPopover.style.top = "0px";
  entry.listingsPopover.style.bottom = "auto";
  entry.listingsPopover.style.zIndex = "12000";
  entry.listingsPopoverPositionLocked = false;

  // Position first (while still hidden), then open on next frame so the animation
  // originates from the hover area instead of gliding in from (0, 0).
  positionGlobalListingsPopover(entry, { force: true });
  window.requestAnimationFrame(() => {
    entry.listingsPopover.classList.add("listings-popover--global-open");
    entry.listingsPopoverPositionLocked = true;
  });
}

function unmountListingsPopoverFromBody(entry) {
  if (!entry.listingsPopoverMountedToBody) {
    return;
  }

  // Always reset the popover even if something removed the placeholder.
  entry.listingsPopoverMountedToBody = false;
  entry.listingsPopoverPositionLocked = false;
  entry.listingsPopover.classList.remove("listings-popover--global-open", "listings-popover--global");
  entry.listingsPopover.removeAttribute("style");

  if (entry.listingsPopoverHome && entry.listingsPopoverPlaceholder?.parentNode) {
    entry.listingsPopoverPlaceholder.parentNode.replaceChild(entry.listingsPopover, entry.listingsPopoverPlaceholder);
  }
}

function positionGlobalListingsPopover(entry, options = {}) {
  if (!entry.listingsPopoverMountedToBody) return;
  const { force = false } = options;
  if (!force && entry.listingsPopoverPositionLocked) {
    return;
  }
  const hoverEl = entry.listingsHoverArea;
  const popoverEl = entry.listingsPopover;
  if (!hoverEl || !popoverEl) return;

  const viewportWidth = document.documentElement?.clientWidth || window.innerWidth;
  const margin = 8;
  const gap = 8;
  const hoverRect = hoverEl.getBoundingClientRect();

  const popoverRect = popoverEl.getBoundingClientRect();
  const width = popoverRect.width || Math.min(320, window.innerWidth * 0.88);
  const idealCenterX = hoverRect.left + hoverRect.width / 2;
  const minCenterX = margin + width / 2;
  const maxCenterX = viewportWidth - margin - width / 2;
  const centerX = Math.max(minCenterX, Math.min(maxCenterX, idealCenterX));

  // Avoid CSS transforms for positioning on mobile because they can make text
  // look slightly blurry. Use pixel positioning instead.
  const docCenterX = centerX + window.scrollX;
  const docTopY = hoverRect.top - gap + window.scrollY;

  const height = popoverRect.height || 0;
  const left = docCenterX - width / 2;
  const top = docTopY - height;
  popoverEl.style.left = `${Math.round(left)}px`;
  popoverEl.style.top = `${Math.round(top)}px`;
  popoverEl.style.transform = "none";
}

function positionListingsPopover(entry) {
  const margin = 8;
  const popoverEl = entry.listingsPopover;
  const hoverEl = entry.listingsHoverArea;
  if (!popoverEl || !hoverEl) {
    return;
  }

  if (entry.listingsPopoverMountedToBody) {
    positionGlobalListingsPopover(entry);
    return;
  }

  // Desktop: popover is positioned using `left: 50%` + translateX(-50%) and a shift-x CSS var.
  // Clamp the visual center so it never overflows the viewport (fixes rightmost cards).
  const viewportWidth = document.documentElement?.clientWidth || window.innerWidth;
  const hoverRect = hoverEl.getBoundingClientRect();
  const popoverRect = popoverEl.getBoundingClientRect();
  const width = popoverRect.width || popoverEl.offsetWidth || Math.min(320, viewportWidth * 0.88);

  const idealCenterX = hoverRect.left + hoverRect.width / 2;
  const minCenterX = margin + width / 2;
  const maxCenterX = viewportWidth - margin - width / 2;
  const clampedCenterX = Math.max(minCenterX, Math.min(maxCenterX, idealCenterX));

  const shiftX = clampedCenterX - idealCenterX;
  hoverEl.style.setProperty("--listings-popover-shift-x", `${Math.round(shiftX)}px`);
}

function setListingsPopoverBody(entry, message, className = "") {
  clearNodeChildren(entry.listingsPopoverBody);
  const line = document.createElement("div");
  line.className = `listings-popover-message ${className}`.trim();
  line.textContent = message;
  entry.listingsPopoverBody.appendChild(line);
}

function formatFetchedAtMinutesAgo(updatedAt) {
  const parsed = typeof updatedAt === "string" ? new Date(updatedAt) : null;
  const now = Date.now();
  const then = parsed && !Number.isNaN(parsed.getTime()) ? parsed.getTime() : now;
  const diffMs = Math.max(0, now - then);
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin === 0) return "just now";
  if (diffMin === 1) return "1 minute ago";
  return `${diffMin} minutes ago`;
}

function setListingsPopoverHeader(entry, payload) {
  if (!payload) {
    entry.listingsPopoverHeader.textContent = "Listings";
    return;
  }
  const minutesAgo = formatFetchedAtMinutesAgo(payload.updatedAt);
  entry.listingsPopoverHeader.textContent = `Listings (fetched ${minutesAgo})`;
}

function setListingsPreviewSubline(entry, payload) {
  if (!payload) {
    entry.listingsPopoverSubline.textContent = "";
    return;
  }

  const total = Number.isFinite(payload.totalResults) ? payload.totalResults : null;
  const leagueLabel = payload.league || "Standard";
  if (total == null) {
    entry.listingsPopoverSubline.textContent = `Top live listings (${leagueLabel})`;
    return;
  }

  entry.listingsPopoverSubline.textContent = `${total} total listings (${leagueLabel})`;
}

function aggregateListingsForDisplay(listings) {
  const out = [];
  const byKey = new Map();

  for (const row of Array.isArray(listings) ? listings : []) {
    if (!row || typeof row !== "object") continue;

    const seller = row.sellerName != null ? String(row.sellerName) : "";
    const amount = row.amount != null ? Number(row.amount) : null;
    const currency = row.currency != null ? String(row.currency).trim().toLowerCase() : "";
    const corrupted = !!row.corrupted;
    const instant = !!row.isInstantBuyout;
    const count = Number(row.listingCount) > 0 ? Math.floor(Number(row.listingCount)) : 1;
    const key = [
      seller,
      currency,
      amount != null && Number.isFinite(amount) ? amount.toFixed(6) : "",
      corrupted ? "1" : "0",
      instant ? "1" : "0",
    ].join("|");

    const idx = byKey.get(key);
    if (idx == null) {
      byKey.set(key, out.length);
      out.push({ ...row, count });
      continue;
    }
    out[idx].count += count;
  }

  return out;
}

function renderListingsPreview(entry, payload) {
  const previousScrollTop = entry.listingsPopover ? entry.listingsPopover.scrollTop : 0;

  setListingsPopoverHeader(entry, payload);
  setListingsPreviewSubline(entry, payload);
  clearNodeChildren(entry.listingsPopoverBody);

  const listings = aggregateListingsForDisplay(payload?.listings);
  if (!listings.length) {
    setListingsPopoverBody(entry, "No priced listings returned.", "listings-popover-muted");
    if (entry.listingsPopover) {
      entry.listingsPopover.scrollTop = previousScrollTop;
    }
    return;
  }

  const list = document.createElement("div");
  list.className = "listings-popover-list";

  for (let i = 0; i < listings.length; i += 1) {
    const rowData = listings[i];
    const row = document.createElement("div");
    row.className = "listings-popover-row";

    const top = document.createElement("div");
    top.className = "listings-row-top";

    const price = document.createElement("span");
    price.className = "listings-row-price";
    const amountText = rowData.amount != null ? rowData.amount : null;
    const currencyText = rowData.currency != null ? rowData.currency : null;
    if (amountText == null || currencyText == null) {
      price.textContent = rowData.priceText || "No listed price";
    } else {
      const amountEl = document.createElement("span");
      amountEl.className = "listings-row-amount";
      amountEl.textContent = String(amountText);

      const iconPath = currencyIconPath(currencyText);
      if (iconPath) {
        const icon = document.createElement("img");
        icon.className = "listings-row-currency";
        icon.src = iconPath;
        icon.alt = String(currencyText);
        icon.decoding = "async";
        icon.loading = "lazy";
        icon.width = 18;
        icon.height = 18;
        price.appendChild(amountEl);
        price.appendChild(icon);
      } else {
        price.appendChild(amountEl);
      }

      const currencyEl = document.createElement("span");
      currencyEl.className = "listings-row-currency-text";
      currencyEl.textContent = formatCurrencyLabel(currencyText);
      price.appendChild(currencyEl);
    }

    const buyout = document.createElement("span");
    buyout.className = `buyout-badge ${rowData.isInstantBuyout ? "yes" : "no"}`;
    buyout.textContent = rowData.isInstantBuyout ? "Instant trade" : "In-person trade";

    const priceGroup = document.createElement("span");
    priceGroup.className = "listings-row-price-group";
    priceGroup.appendChild(price);
    if (rowData.count > 1) {
      const countBadge = document.createElement("span");
      countBadge.className = "listings-row-count-badge";
      countBadge.textContent = `x${rowData.count}`;
      priceGroup.appendChild(countBadge);
    }

    top.appendChild(priceGroup);
    const badgesGroup = document.createElement("span");
    badgesGroup.className = "listings-row-badges-group";
    if (rowData.corrupted) {
      const corruptBadge = document.createElement("span");
      corruptBadge.className = "listings-row-corrupt-badge";
      corruptBadge.textContent = "C";
      corruptBadge.setAttribute("aria-label", "Corrupted");
      corruptBadge.title = "Corrupted";
      badgesGroup.appendChild(corruptBadge);
    }
    badgesGroup.appendChild(buyout);
    top.appendChild(badgesGroup);

    const meta = document.createElement("div");
    meta.className = "listings-row-meta";

    const seller = document.createElement("span");
    const sellerName = rowData.sellerName || "unknown seller";
    seller.textContent = sellerName;
    if (rowData.count > 1) {
      seller.title = `${sellerName} (${rowData.count} listings)`;
    }

    const posted = document.createElement("span");
    posted.textContent = rowData.posted || "unknown";

    meta.append(seller, posted);
    row.append(top, meta);
    list.appendChild(row);
  }

  entry.listingsPopoverBody.appendChild(list);

  if (entry.listingsPopover) {
    entry.listingsPopover.scrollTop = previousScrollTop;
  }
}

async function fetchListingsPreview(queryId) {
  const cached = listingsPreviewCache.get(queryId);
  // Be defensive: cache entries can be missing/corrupt during hot reloads or partial updates.
  if (
    cached &&
    typeof cached === "object" &&
    Number.isFinite(cached.fetchedAt) &&
    Date.now() - cached.fetchedAt < LISTINGS_PREVIEW_CACHE_TTL_MS
  ) {
    return cached?.payload ?? null;
  }

  if (listingsPreviewInFlight.has(queryId)) {
    return listingsPreviewInFlight.get(queryId);
  }

  const promise = fetch(`/api/listings?queryId=${encodeURIComponent(queryId)}`, { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return response.json();
    })
    .then((payload) => {
      if (!payload || typeof payload !== "object") {
        return null;
      }
      const source = typeof payload?.source === "string" ? payload.source : "";
      const isRetryableMiss = source === "cache-miss" || source === "cache-not-found" || source === "cache-read-error";
      if (!isRetryableMiss) {
        listingsPreviewCache.set(queryId, { payload, fetchedAt: Date.now() });
      }
      return payload;
    })
    .finally(() => {
      listingsPreviewInFlight.delete(queryId);
    });

  listingsPreviewInFlight.set(queryId, promise);
  return promise;
}

async function loadListingsPreview(entry, options = {}) {
  const { force = false, silent = false } = options;
  const queryId = entry.currentQueryId;
  if (!queryId || (!force && queryId === entry.loadedQueryId) || entry.loadingQueryId === queryId) {
    return;
  }

  entry.loadingQueryId = queryId;
  if (!silent) {
    setListingsPopoverBody(entry, "Loading live listings...", "listings-popover-muted");
  }

  try {
    const payload = await fetchListingsPreview(queryId);
    if (entry.currentQueryId !== queryId) {
      return;
    }

    if (!payload) {
      setListingsPopoverBody(entry, "Could not load listings (no data returned).", "listings-popover-error");
      return;
    }

    renderListingsPreview(entry, payload);
    entry.loadedQueryId = queryId;
  } catch (error) {
    if (entry.currentQueryId !== queryId) {
      return;
    }

    const message = error instanceof Error ? error.message : "Unknown error";
    setListingsPopoverBody(entry, `Could not load listings (${message}).`, "listings-popover-error");
  } finally {
    if (entry.loadingQueryId === queryId) {
      entry.loadingQueryId = null;
    }
  }
}

function startListingsLiveRefresh(entry) {
  if (entry.listingsRefreshTimer != null) {
    return;
  }

  entry.listingsRefreshTimer = window.setInterval(() => {
    if (!entry.currentQueryId || entry.listingsHoverArea.classList.contains("disabled")) {
      return;
    }

    if (!entry.listingsHoverArea.classList.contains("popover-open")) {
      return;
    }

    void loadListingsPreview(entry, { force: true, silent: true });
  }, REFRESH_MS);
}

export function stopListingsPopover(entry) {
  if (entry.listingsRefreshTimer != null) {
    window.clearInterval(entry.listingsRefreshTimer);
    entry.listingsRefreshTimer = null;
  }
  if (entry.listingsPopoverCloseTimer != null) {
    window.clearTimeout(entry.listingsPopoverCloseTimer);
    entry.listingsPopoverCloseTimer = null;
  }
  closeGlobalListingsOverlay(entry.closePopover);
  unmountListingsPopoverFromBody(entry);
  entry.listingsHoverArea?.classList?.remove("popover-open");
  entry.card?.classList?.remove("popover-active");
  if (entry.handleViewportChange) {
    window.removeEventListener("resize", entry.handleViewportChange);
  }
}

export function wireListingsPopover(entry) {
  const { card, listingsHoverArea, listingsPopover } = entry;
  if (!card || !listingsHoverArea || !listingsPopover) {
    return;
  }

  const triggerListingsPreviewLoad = () => {
    if (listingsHoverArea.classList.contains("disabled")) {
      return;
    }
    void loadListingsPreview(entry);
  };

  entry.requestListingsPreviewLoad = triggerListingsPreviewLoad;

  const cancelScheduledClose = () => {
    if (entry.listingsPopoverCloseTimer != null) {
      window.clearTimeout(entry.listingsPopoverCloseTimer);
      entry.listingsPopoverCloseTimer = null;
    }
  };

  const scheduleClosePopoverDesktop = () => {
    if (isMobileViewport()) return;
    cancelScheduledClose();
    entry.listingsPopoverCloseTimer = window.setTimeout(() => {
      entry.listingsPopoverCloseTimer = null;
      closePopover();
    }, LISTINGS_POPOVER_CLOSE_DELAY_MS);
  };

  const openPopover = () => {
    cancelScheduledClose();
    triggerListingsPreviewLoad();
    listingsHoverArea.classList.add("popover-open");
    if (!isMobileViewport()) {
      card.classList.add("popover-active");
    } else {
      mountListingsPopoverToBody(entry);
      positionListingsPopover(entry);
    }
    startListingsLiveRefresh(entry);
    openGlobalListingsOverlay(closePopover);
    if (!entry.handleViewportChange) {
      entry.handleViewportChange = () => {
        if (!entry.listingsHoverArea.classList.contains("popover-open")) {
          return;
        }
        positionGlobalListingsPopover(entry, { force: true });
      };
    }
    window.addEventListener("resize", entry.handleViewportChange);
    window.requestAnimationFrame(() => {
      positionListingsPopover(entry);
    });
  };

  const closePopover = () => {
    cancelScheduledClose();
    listingsHoverArea.classList.remove("popover-open");
    card.classList.remove("popover-active");
    if (entry.listingsRefreshTimer != null) {
      window.clearInterval(entry.listingsRefreshTimer);
      entry.listingsRefreshTimer = null;
    }
    closeGlobalListingsOverlay(closePopover);
    unmountListingsPopoverFromBody(entry);
    listingsHoverArea.style.setProperty("--listings-popover-shift-x", "0px");
    if (entry.handleViewportChange) {
      window.removeEventListener("resize", entry.handleViewportChange);
    }
  };

  entry.openPopover = openPopover;
  entry.closePopover = closePopover;

  listingsHoverArea.addEventListener("mouseenter", openPopover);
  listingsHoverArea.addEventListener("focusin", triggerListingsPreviewLoad);

  const togglePopoverFromTap = (event) => {
    if (!isMobileViewport()) return;
    if (listingsHoverArea.classList.contains("disabled")) return;
    event.preventDefault();
    event.stopPropagation();
    if (listingsHoverArea.classList.contains("popover-open")) {
      closePopover();
    } else {
      openPopover();
    }
  };

  if ("PointerEvent" in window) {
    // On mobile, a scroll gesture starts with a pointerdown; toggling on pointerdown
    // causes the popover to "open while scrolling". Treat it as a tap only if the
    // pointer doesn't move beyond a small threshold before release.
    let tapPointerId = null;
    let tapStartX = 0;
    let tapStartY = 0;
    let tapMoved = false;
    const TAP_MOVE_PX = 10;

    const onPointerDown = (event) => {
      if (!isMobileViewport()) return;
      if (listingsHoverArea.classList.contains("disabled")) return;
      if (event.pointerType === "mouse") return;
      tapPointerId = event.pointerId;
      tapStartX = event.clientX;
      tapStartY = event.clientY;
      tapMoved = false;
    };

    const onPointerMove = (event) => {
      if (tapPointerId == null) return;
      if (event.pointerId !== tapPointerId) return;
      const dx = event.clientX - tapStartX;
      const dy = event.clientY - tapStartY;
      if (Math.hypot(dx, dy) > TAP_MOVE_PX) {
        tapMoved = true;
      }
    };

    const onPointerUp = (event) => {
      if (tapPointerId == null) return;
      if (event.pointerId !== tapPointerId) return;
      const shouldToggle = !tapMoved;
      tapPointerId = null;
      if (!shouldToggle) return;
      togglePopoverFromTap(event);
    };

    const onPointerCancel = (event) => {
      if (tapPointerId == null) return;
      if (event.pointerId !== tapPointerId) return;
      tapPointerId = null;
    };

    listingsHoverArea.addEventListener("pointerdown", onPointerDown, { passive: true });
    listingsHoverArea.addEventListener("pointermove", onPointerMove, { passive: true });
    listingsHoverArea.addEventListener("pointerup", onPointerUp, { passive: false });
    listingsHoverArea.addEventListener("pointercancel", onPointerCancel, { passive: true });
  } else {
    listingsHoverArea.addEventListener("click", togglePopoverFromTap);
  }

  listingsHoverArea.addEventListener("mouseleave", () => {
    if (isMobileViewport()) return;
    scheduleClosePopoverDesktop();
  });

  listingsPopover.addEventListener("mouseenter", () => {
    if (isMobileViewport()) return;
    cancelScheduledClose();
    listingsHoverArea.classList.add("popover-open");
    card.classList.add("popover-active");
    positionListingsPopover(entry);
  });

  listingsPopover.addEventListener("mouseleave", () => {
    if (isMobileViewport()) return;
    scheduleClosePopoverDesktop();
  });
}

