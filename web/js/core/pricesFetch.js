import { REFRESH_MS, buildPricesApiUrl } from "./state.js";

let inFlight = null;
let cachedUrl = "";
let cachedPayload = null;
let cachedAt = 0;

/**
 * Fetch /api/prices with in-flight dedupe and a short-lived client cache so
 * multiple modules/tabs worth of logic do not hammer the server on the same tick.
 */
export async function fetchPricesPayload({ force = false, url: urlOverride = null } = {}) {
  const url = urlOverride ?? buildPricesApiUrl();
  const now = Date.now();
  if (!force && cachedUrl === url && cachedPayload && now - cachedAt < REFRESH_MS) {
    return cachedPayload;
  }
  if (inFlight?.url === url) {
    return inFlight.promise;
  }

  const promise = (async () => {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    cachedUrl = url;
    cachedPayload = payload;
    cachedAt = Date.now();
    return payload;
  })();

  inFlight = { url, promise };
  try {
    return await promise;
  } finally {
    if (inFlight?.promise === promise) {
      inFlight = null;
    }
  }
}
