/**
 * Fetch the latest poller position + divine:mirror ratio (uncached, cheap).
 */
export async function fetchPollStatus() {
  const response = await fetch("/api/poll-status", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}
