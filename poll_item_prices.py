from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://www.pathofexile.com/api/trade"
DEFAULT_LEAGUE = "Standard"
DEFAULT_ITEMS_FILE = "items.txt"
DEFAULT_OUTPUT_FILE = "price_poll.csv"
DEFAULT_CONFIG_FILE = "config.json"
TOP_IDS_LIMIT = 5
DIVINES_PER_MIRROR = 1650.0
# All timestamps are stored in UTC (Coordinated Universal Time) in ISO 8601 format.
# When displayed to users via the web dashboard, they are automatically converted to local time.
CSV_HEADER = [
    "timestamp_utc",  # ISO 8601 format, always UTC
    "cycle",
    "item_name",
    "item_mode",
    "query_id",
    "total_results",
    "used_results",
    "unsupported_price_count",
    "mirror_count",
    "lowest_mirror",
    "median_mirror",
    "highest_mirror",
    "divine_count",
    "lowest_divine",
    "median_divine",
    "highest_divine",
]

# Proactive safety margin over the natural allowed request pace.
RATE_LIMIT_SAFETY = 1.1
LOW_HEADROOM_THRESHOLD = 0.15
VERY_LOW_HEADROOM_THRESHOLD = 0.08
# Keep a reserve budget per window so this script leaves room for manual trade use.
RESERVE_RATIO = 0.20


@dataclass
class Config:
    poll_interval: int
    max_cycles: int | None


@dataclass
class AlertConfig:
    enabled: bool
    threshold_pct: float
    history_cycles: int
    webhook_url: str


def load_alert_config() -> AlertConfig:
    path = Path(DEFAULT_CONFIG_FILE)
    if not path.exists():
        return AlertConfig(enabled=False, threshold_pct=30.0, history_cycles=10, webhook_url="")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return AlertConfig(
            enabled=bool(data.get("alert_enabled", False)),
            threshold_pct=float(data.get("alert_threshold_pct", 30.0)),
            history_cycles=max(1, int(data.get("alert_history_cycles", 10))),
            webhook_url=str(data.get("discord_webhook_url", "")),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: Could not load alert config: {exc}")
        return AlertConfig(enabled=False, threshold_pct=30.0, history_cycles=10, webhook_url="")


def seed_price_history(output_csv: Path, history_cycles: int) -> dict[str, list[float]]:
    """Prime in-memory price history from CSV row order (newest rows win naturally)."""
    history: dict[str, list[float]] = {}
    if not output_csv.exists():
        return history
    try:
        with output_csv.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                item_name = (row.get("item_name") or "").strip()
                item_mode = (row.get("item_mode") or "").strip()
                median_raw = (row.get("median_mirror") or "").strip()
                if not item_name or not item_mode or not median_raw:
                    continue
                try:
                    key = f"{item_name}::{item_mode}"
                    history.setdefault(key, []).append(float(median_raw))
                except ValueError:
                    continue

        for key, values in list(history.items()):
            history[key] = values[-history_cycles:]
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: Could not seed price history from CSV: {exc}")
    return history


def send_discord_alert(
    alert_session: requests.Session,
    item: ItemSpec,
    current_lowest: float,
    baseline: float,
    pct_drop: float,
    query_id: str,
    webhook_url: str,
    item_image_url: str | None = None,
) -> None:
    trade_url = f"https://www.pathofexile.com/trade/search/{DEFAULT_LEAGUE}/{query_id}"
    embed = {
        "title": f"\N{BELL} Price Alert: {item.name}",
        "description": (
            f"Listed at **{format_amount(current_lowest)} mirrors** \u2014 "
            f"**{pct_drop:.1f}% below** baseline of {format_amount(baseline)} mirrors"
        ),
        "color": 0xFF6B35,
        "fields": [
            {
                "name": "Trade Link",
                "value": f"[View listing]({trade_url})",
                "inline": False,
            }
        ],
    }
    if item_image_url:
        embed["thumbnail"] = {"url": item_image_url}
    try:
        resp = alert_session.post(
            webhook_url,
            json={"content": "@everyone", "embeds": [embed]},
            timeout=10.0,
        )
        resp.raise_for_status()
        print(f"Discord alert sent for {item.name} ({pct_drop:.1f}% below baseline)")
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: Failed to send Discord alert for {item.name}: {exc}")


@dataclass
class ItemSpec:
    name: str
    alternate_art: bool | None


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously poll PoE trade prices for unique items listed in items.txt and append summaries to CSV."
        )
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=3600,
        help="Poll interval in seconds; scheduling stays aligned to start-time grid",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Stop after this many cycles (omit to run indefinitely)",
    )

    args = parser.parse_args()

    if args.poll_interval <= 0:
        raise SystemExit("--poll-interval must be > 0")
    if args.max_cycles is not None and args.max_cycles <= 0:
        raise SystemExit("--max-cycles must be > 0")

    return Config(poll_interval=args.poll_interval, max_cycles=args.max_cycles)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
    )
    return session


def parse_rate_entry(raw: str) -> tuple[int, int, int] | None:
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    try:
        a = int(parts[0])
        b = int(parts[1])
        c = int(parts[2])
    except ValueError:
        return None
    return a, b, c


@dataclass
class RateWindowState:
    source: str
    max_requests: int
    window_seconds: int
    used_requests: int
    retry_after_seconds: int


class AdaptiveRateLimiter:
    """Tracks live rate-limit headers and adjusts pacing before each request."""

    def __init__(self) -> None:
        self._next_allowed_at = 0.0
        self._windows: list[RateWindowState] = []

    def wait_before_request(self) -> None:
        now = time.monotonic()
        if self._next_allowed_at <= now:
            return

        sleep_for = self._next_allowed_at - now
        if sleep_for > 0:
            time.sleep(sleep_for)

    def update_from_response(
        self,
        headers: requests.structures.CaseInsensitiveDict[str],
        status_code: int,
    ) -> None:
        windows = self._parse_windows(headers)
        if not windows:
            return

        self._windows = windows
        observed_at = time.monotonic()
        wait_seconds = self._compute_wait_seconds(
            windows=windows,
            status_code=status_code,
            retry_after_header=self._parse_retry_after(headers.get("retry-after")),
        )
        self._next_allowed_at = max(self._next_allowed_at, observed_at + wait_seconds)

        self._log_live_status(windows, wait_seconds, status_code)

    def _parse_retry_after(self, raw: str | None) -> int | None:
        if raw is None:
            return None
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            return None
        return max(0, value)

    def _parse_windows(
        self, headers: requests.structures.CaseInsensitiveDict[str]
    ) -> list[RateWindowState]:
        windows: list[RateWindowState] = []

        account_limits = headers.get("x-rate-limit-account")
        account_states = headers.get("x-rate-limit-account-state")
        windows.extend(self._parse_source_windows("account", account_limits, account_states))

        ip_limits = headers.get("x-rate-limit-ip")
        ip_states = headers.get("x-rate-limit-ip-state")
        windows.extend(self._parse_source_windows("ip", ip_limits, ip_states))

        return windows

    def _parse_source_windows(
        self,
        source: str,
        limits_raw: str | None,
        states_raw: str | None,
    ) -> list[RateWindowState]:
        if not limits_raw or not states_raw:
            return []

        limits = [part.strip() for part in limits_raw.split(",") if part.strip()]
        states = [part.strip() for part in states_raw.split(",") if part.strip()]
        pair_count = min(len(limits), len(states))
        if pair_count == 0:
            return []

        out: list[RateWindowState] = []
        for i in range(pair_count):
            limit_entry = parse_rate_entry(limits[i])
            state_entry = parse_rate_entry(states[i])
            if not limit_entry or not state_entry:
                continue

            max_requests, window_seconds, _ = limit_entry
            used_requests, _, retry_after_seconds = state_entry

            if max_requests <= 0 or window_seconds <= 0:
                continue

            out.append(
                RateWindowState(
                    source=source,
                    max_requests=max_requests,
                    window_seconds=window_seconds,
                    used_requests=max(0, used_requests),
                    retry_after_seconds=max(0, retry_after_seconds),
                )
            )

        return out

    def _compute_wait_seconds(
        self,
        windows: list[RateWindowState],
        status_code: int,
        retry_after_header: int | None,
    ) -> float:
        required_wait = 0.0
        hard_block_wait = 0.0

        for window in windows:
            if window.max_requests <= 0:
                continue

            natural_delay = (window.window_seconds / window.max_requests) * RATE_LIMIT_SAFETY
            usage_ratio = min(1.0, window.used_requests / window.max_requests)
            remaining = max(0, window.max_requests - window.used_requests)
            headroom_ratio = remaining / window.max_requests
            reserve_budget = self._reserve_budget(window.max_requests)

            adaptive_factor = 1.0 + usage_ratio
            candidate_wait = natural_delay * adaptive_factor

            if remaining <= reserve_budget:
                # Preemptively slow down when we're close to reserved capacity.
                candidate_wait = max(candidate_wait, natural_delay * 4.0)
                if window.retry_after_seconds > 0:
                    candidate_wait = max(
                        candidate_wait,
                        float(window.retry_after_seconds) + 0.05,
                    )

            if headroom_ratio <= VERY_LOW_HEADROOM_THRESHOLD:
                candidate_wait = max(candidate_wait, natural_delay * 3.0)
            elif headroom_ratio <= LOW_HEADROOM_THRESHOLD:
                candidate_wait = max(candidate_wait, natural_delay * 2.0)

            # The third state value can be non-zero even when not currently blocked.
            # Treat it as hard lockout only on exhausted windows or explicit 429.
            is_exhausted = window.used_requests >= window.max_requests
            if is_exhausted and window.retry_after_seconds > 0:
                hard_block_wait = max(hard_block_wait, float(window.retry_after_seconds) + 0.05)

            required_wait = max(required_wait, candidate_wait)

        if status_code == 429:
            if retry_after_header is not None and retry_after_header > 0:
                hard_block_wait = max(hard_block_wait, float(retry_after_header) + 0.05)
            for window in windows:
                if window.retry_after_seconds > 0:
                    hard_block_wait = max(hard_block_wait, float(window.retry_after_seconds) + 0.05)

        required_wait = max(required_wait, hard_block_wait)
        return required_wait

    def _reserve_budget(self, max_requests: int) -> int:
        if max_requests <= 1:
            return 0

        reserve = max(1, math.ceil(max_requests * RESERVE_RATIO))
        return min(max_requests - 1, reserve)

    def _log_live_status(self, windows: list[RateWindowState], next_wait: float, status_code: int) -> None:
        parts: list[str] = []
        for window in windows:
            remaining = max(0, window.max_requests - window.used_requests)
            parts.append(
                f"{window.source} {window.used_requests}/{window.max_requests} "
                f"(rem={remaining}, retry={window.retry_after_seconds}s)"
            )

        summary = " | ".join(parts)
        print(
            f"Rate monitor: status={status_code} {summary}; "
            f"next request in ~{next_wait:.2f}s"
        )


def parse_item_spec_line(line: str) -> ItemSpec:
    """Parse one items.txt line.

    Supported formats:
    - Item Name               -> alternate art only (default)
    - Item Name|aa            -> alternate art only
    - Item Name|normal        -> non-alternate-art only
    - Item Name|any           -> either alt or non-alt
    """
    parts = [part.strip() for part in line.split("|", 1)]
    name = parts[0]
    if not name:
        raise RuntimeError("Invalid item line: missing item name")

    if len(parts) == 1:
        return ItemSpec(name=name, alternate_art=True)

    mode = parts[1].lower()
    if mode in {"aa", "alt", "alternate", "alternate-art", "alternate_art", "true"}:
        return ItemSpec(name=name, alternate_art=True)
    if mode in {"normal", "non-alt", "non_alt", "regular", "false"}:
        return ItemSpec(name=name, alternate_art=False)
    if mode in {"any", "either", "all"}:
        return ItemSpec(name=name, alternate_art=None)

    raise RuntimeError(
        "Invalid mode in items.txt line. Use one of: aa, normal, any. "
        f"Got: '{parts[1]}'"
    )


def load_item_specs(items_file: Path) -> list[ItemSpec]:
    if not items_file.exists():
        raise RuntimeError(f"Items file not found: {items_file}")

    specs: list[ItemSpec] = []
    with items_file.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            specs.append(parse_item_spec_line(line))

    if not specs:
        raise RuntimeError(f"No item names found in {items_file}")

    return specs


def build_search_payload(item: ItemSpec) -> dict[str, Any]:
    name_option: dict[str, Any] = {"option": item.name}
    # name_option["discriminator"] = "legacy"

    misc_filters: dict[str, Any] = {
        "mirrored": {"option": "false"},
    }
    if item.alternate_art is True:
        misc_filters["alternate_art"] = {"option": "true"}
    elif item.alternate_art is False:
        misc_filters["alternate_art"] = {"option": "false"}

    query: dict[str, Any] = {
        "status": {"option": "available"},
        "name": name_option,
        "stats": [{"type": "and", "filters": [], "disabled": False}],
        "filters": {
            "misc_filters": {
                "filters": misc_filters,
                "disabled": False,
            }
        },
    }

    return {
        "query": query,
        "sort": {"price": "asc"},
    }


def search_item(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    item: ItemSpec,
) -> tuple[str, list[str], int]:
    url = f"{BASE_URL}/search/{DEFAULT_LEAGUE}"
    payload = build_search_payload(item)

    rate_limiter.wait_before_request()
    response = session.post(url, data=json.dumps(payload), timeout=30.0)
    rate_limiter.update_from_response(response.headers, response.status_code)
    response.raise_for_status()

    data = response.json()
    query_id = data.get("id")
    result_ids = data.get("result", [])
    total = data.get("total", 0)

    if not isinstance(query_id, str) or not query_id:
        raise RuntimeError(f"Search did not return an id for item '{item.name}'")
    if not isinstance(result_ids, list):
        raise RuntimeError(f"Search returned malformed result IDs for item '{item.name}'")
    if not isinstance(total, int):
        total = len(result_ids)

    return query_id, result_ids, total


def fetch_top_listings(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    query_id: str,
    result_ids: list[str],
) -> list[dict[str, Any]]:
    top_ids = [x for x in result_ids[:TOP_IDS_LIMIT] if isinstance(x, str)]
    if not top_ids:
        return []

    ids_joined = ",".join(top_ids)
    url = f"{BASE_URL}/fetch/{ids_joined}"
    rate_limiter.wait_before_request()
    response = session.get(url, params={"query": query_id}, timeout=30.0)
    rate_limiter.update_from_response(response.headers, response.status_code)
    response.raise_for_status()

    data = response.json()
    result = data.get("result", [])
    if not isinstance(result, list):
        raise RuntimeError("Fetch endpoint returned malformed result data")
    return result


def normalize_price_currency(price: dict[str, Any]) -> tuple[str, float] | None:
    amount = price.get("amount")
    currency = price.get("currency")

    if not isinstance(amount, (int, float)):
        return None
    if not isinstance(currency, str):
        return None

    normalized_currency = currency.strip().lower()
    if normalized_currency in {"mirror", "mirrors", "mirror of kalandra"}:
        return "mirror", float(amount)
    if normalized_currency in {"divine", "divines", "div", "divine orb", "divine orbs"}:
        return "divine", float(amount)

    return None


def extract_listing_prices(listings: list[dict[str, Any]]) -> tuple[list[float], list[float], int]:
    mirror_prices: list[float] = []
    divine_prices: list[float] = []
    unsupported_count = 0

    for entry in listings:
        listing = entry.get("listing") if isinstance(entry, dict) else None
        price = listing.get("price") if isinstance(listing, dict) else None
        if not isinstance(price, dict):
            unsupported_count += 1
            continue

        normalized = normalize_price_currency(price)
        if normalized is None:
            unsupported_count += 1
            continue

        currency, amount = normalized
        if currency == "mirror":
            mirror_prices.append(amount)
        elif currency == "divine":
            divine_prices.append(amount)

    return mirror_prices, divine_prices, unsupported_count


def find_cheapest_listing_icon(
    listings: list[dict[str, Any]],
    divines_per_mirror: float,
) -> str | None:
    """Return icon URL for the cheapest valid listing, with a best-effort fallback."""
    cheapest_icon: str | None = None
    cheapest_price_mirror: float | None = None
    fallback_icon: str | None = None

    for entry in listings:
        if not isinstance(entry, dict):
            continue

        listing = entry.get("listing")
        price = listing.get("price") if isinstance(listing, dict) else None
        normalized = normalize_price_currency(price) if isinstance(price, dict) else None
        if normalized is None:
            continue

        currency, amount = normalized
        amount_mirror = to_mirror_equivalent(amount, currency, divines_per_mirror)

        item_data = entry.get("item")
        icon_url = item_data.get("icon") if isinstance(item_data, dict) else None
        if not isinstance(icon_url, str) or not icon_url:
            icon_url = None

        if icon_url and fallback_icon is None:
            fallback_icon = icon_url

        if cheapest_price_mirror is None or amount_mirror < cheapest_price_mirror:
            cheapest_price_mirror = amount_mirror
            cheapest_icon = icon_url

    return cheapest_icon or fallback_icon


def summarize_prices(prices: list[float]) -> tuple[float | None, float | None, float | None]:
    if not prices:
        return None, None, None

    low = min(prices)
    high = max(prices)
    median = statistics.median(prices)
    return low, high, median


def format_amount(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def to_mirror_equivalent(amount: float, currency: str, divines_per_mirror: float = DIVINES_PER_MIRROR) -> float:
    if currency == "mirror":
        return amount
    if currency == "divine":
        return amount / divines_per_mirror
    raise ValueError(f"Unsupported currency: {currency}")


def build_mirror_price_payload() -> dict[str, Any]:
    """Bulk exchange payload: buying mirrors with divines, sorted cheapest first."""
    return {
        "query": {
            "status": {"option": "online"},
            "want": ["mirror"],
            "have": ["divine"],
        },
        "sort": {"have": "asc"},
        "engine": "new",
    }


def fetch_mirror_divine_median(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
) -> float | None:
    """Fetch top 5 Mirror of Kalandra bulk-exchange listings and return the median divine price."""
    url = f"{BASE_URL}/exchange/{DEFAULT_LEAGUE}"
    payload = build_mirror_price_payload()

    rate_limiter.wait_before_request()
    response = session.post(url, data=json.dumps(payload), timeout=30.0)
    rate_limiter.update_from_response(response.headers, response.status_code)
    response.raise_for_status()

    data = response.json()
    # Exchange endpoint returns result as a dict {id: listing_data}, not a list.
    result = data.get("result", {})

    if isinstance(result, list):
        # Fallback: some versions return a list of IDs instead.
        entries = result[:TOP_IDS_LIMIT]
    elif isinstance(result, dict):
        all_ids = list(result.keys())[:TOP_IDS_LIMIT]
        entries = [result[k] for k in all_ids]
    else:
        print("Mirror of Kalandra: unexpected result format from exchange endpoint.")
        return None

    if not entries:
        print("Mirror of Kalandra: no exchange listings found.")
        return None

    divine_prices: list[float] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        listing = entry.get("listing")
        if not isinstance(listing, dict):
            continue
        offers = listing.get("offers", [])
        if not isinstance(offers, list) or not offers:
            continue
        offer = offers[0]
        exchange = offer.get("exchange", {})
        item = offer.get("item", {})
        # exchange.amount divines for item.amount mirrors
        ex_amount = exchange.get("amount")
        it_amount = item.get("amount") or 1
        if isinstance(ex_amount, (int, float)) and ex_amount > 0:
            divine_prices.append(float(ex_amount) / float(it_amount))

    if not divine_prices:
        print("Mirror of Kalandra: no valid divine-priced exchange listings in top 5.")
        return None

    median_price = statistics.median(divine_prices)
    print(
        f"Mirror of Kalandra: top-{len(divine_prices)} median = "
        f"{format_amount(median_price)} divine  (ratio updated)"
    )
    return median_price


def item_mode_label(item: ItemSpec) -> str:
    if item.alternate_art is True:
        return "alternate art"
    if item.alternate_art is False:
        return "normal"
    return "any"


def item_mode_token(item: ItemSpec) -> str:
    if item.alternate_art is True:
        return "aa"
    if item.alternate_art is False:
        return "normal"
    return "any"


def ensure_csv_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            current_header = next(reader, None)

        if current_header == CSV_HEADER:
            return

        backup_path = path.with_name(f"{path.stem}.pre-mirror-schema-{int(time.time())}{path.suffix}")
        path.rename(backup_path)
        print(
            f"Existing CSV header did not match current format. "
            f"Backed up old file to {backup_path}."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)


def append_summary_row(
    path: Path,
    timestamp_utc: str,
    cycle: int,
    item: ItemSpec,
    query_id: str,
    total_results: int,
    used_results: int,
    unsupported_price_count: int,
    mirror_count: int,
    lowest_mirror: float | None,
    median_mirror: float | None,
    highest_mirror: float | None,
    divine_count: int,
    lowest_divine: float | None,
    median_divine: float | None,
    highest_divine: float | None,
) -> None:
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                timestamp_utc,
                cycle,
                item.name,
                item_mode_token(item),
                query_id,
                total_results,
                used_results,
                unsupported_price_count,
                mirror_count,
                "" if lowest_mirror is None else f"{lowest_mirror:.4f}",
                "" if median_mirror is None else f"{median_mirror:.4f}",
                "" if highest_mirror is None else f"{highest_mirror:.4f}",
                divine_count,
                "" if lowest_divine is None else f"{lowest_divine:.4f}",
                "" if median_divine is None else f"{median_divine:.4f}",
                "" if highest_divine is None else f"{highest_divine:.4f}",
            ]
        )


def run_cycle(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    output_csv: Path,
    specs: list[ItemSpec],
    cycle: int,
    divines_per_mirror: float = DIVINES_PER_MIRROR,
    price_history: dict[str, list[float]] | None = None,
    alert_config: AlertConfig | None = None,
) -> float:
    """Run one poll cycle; returns the updated divines-per-mirror ratio."""
    # Fetch Mirror of Kalandra price first so the live ratio is ready before item processing.
    new_mirror_ratio = fetch_mirror_divine_median(session, rate_limiter)
    if new_mirror_ratio is not None:
        divines_per_mirror = new_mirror_ratio

    total_items = len(specs)
    max_mode_label_len = max(len(item_mode_label(spec)) for spec in specs)
    max_name_len = max(len(spec.name) for spec in specs)

    for index, item in enumerate(specs, start=1):
        request_timestamp_utc = datetime.now(timezone.utc).isoformat()
        query_id, result_ids, total_results = search_item(session, rate_limiter, item)
        listings = fetch_top_listings(session, rate_limiter, query_id, result_ids)
        cheapest_icon_url = find_cheapest_listing_icon(listings, divines_per_mirror)

        raw_mirror_prices, divine_prices, unsupported_count = extract_listing_prices(listings)

        # Convert all divine-priced listings to their mirror equivalent and merge.
        converted = [p / divines_per_mirror for p in divine_prices]
        mirror_prices = raw_mirror_prices + converted

        low_mirror, high_mirror, median_mirror = summarize_prices(mirror_prices)
        used_results = len(mirror_prices)

        append_summary_row(
            path=output_csv,
            timestamp_utc=request_timestamp_utc,
            cycle=cycle,
            item=item,
            query_id=query_id,
            total_results=total_results,
            used_results=used_results,
            unsupported_price_count=unsupported_count,
            mirror_count=len(mirror_prices),
            lowest_mirror=low_mirror,
            median_mirror=median_mirror,
            highest_mirror=high_mirror,
            divine_count=0,
            lowest_divine=None,
            median_divine=None,
            highest_divine=None,
        )

        cheapest_text = "n/a" if low_mirror is None else f"{format_amount(low_mirror)} mirrors"

        # --- Alert check ---
        if price_history is None or alert_config is None:
            pass  # Skip alert processing if context missing
        elif not alert_config.enabled:
            pass  # Skip alert processing if disabled
        else:
            item_key = f"{item.name}::{item_mode_token(item)}"
            history = price_history.setdefault(item_key, [])

            if median_mirror is not None and low_mirror is not None and len(history) >= alert_config.history_cycles:
                baseline = statistics.median(history[-alert_config.history_cycles:])
                if baseline > 0:
                    pct_drop = (baseline - low_mirror) / baseline * 100
                    if pct_drop >= alert_config.threshold_pct and alert_config.webhook_url:
                        send_discord_alert(
                            session,
                            item,
                            low_mirror,
                            baseline,
                            pct_drop,
                            query_id,
                            alert_config.webhook_url,
                            item_image_url=cheapest_icon_url,
                        )

            # Append median (not lowest) to history — more stable baseline.
            if median_mirror is not None:
                history.append(median_mirror)
                price_history[item_key] = history[-(alert_config.history_cycles * 3):]

        # Console display: use local time for readability (not stored, console-only)
        display_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        index_width = len(str(total_items))
        index_label = f"{index:>{index_width}}/{total_items}"
        mode_label = item_mode_label(item)
        mode_field = f"[{mode_label}]".ljust(max_mode_label_len + 2)
        name_field = f"{item.name:<{max_name_len}}"
        total_field = f"total={total_results:>3}"
        print(
            f"[{index_label}] [{display_time}] {name_field} {mode_field} "
            f"{total_field}   cheapest={cheapest_text}"
        )

    return divines_per_mirror


def seconds_until_next_tick(start_monotonic: float, delay_seconds: int, now_monotonic: float) -> float:
    elapsed = max(0.0, now_monotonic - start_monotonic)
    slot = math.floor(elapsed / delay_seconds) + 1
    next_tick = start_monotonic + (slot * delay_seconds)
    return max(0.0, next_tick - now_monotonic)


def main() -> None:
    cfg = parse_args()
    items_file = Path(DEFAULT_ITEMS_FILE)
    output_csv = Path(DEFAULT_OUTPUT_FILE)
    ensure_csv_header(output_csv)

    session = build_session()
    rate_limiter = AdaptiveRateLimiter()

    item_specs = load_item_specs(items_file)

    print(f"Loaded {len(item_specs)} item(s) from {items_file}. Writing to {output_csv}.")
    print(
        f"Polling every {cfg.poll_interval} seconds aligned to start-time grid. Press Ctrl+C to stop."
    )

    start_monotonic = time.monotonic()
    cycle = 0
    divines_per_mirror = DIVINES_PER_MIRROR

    # Load alert config and seed price history from existing CSV.
    alert_config = load_alert_config()
    price_history = seed_price_history(output_csv, alert_config.history_cycles)
    print(
        f"Alert config: enabled={alert_config.enabled}, "
        f"threshold={alert_config.threshold_pct}%, "
        f"history_cycles={alert_config.history_cycles}"
    )

    while cfg.max_cycles is None or cycle < cfg.max_cycles:
        cycle += 1
        # Console display: use local time for readability (not stored, console-only)
        cycle_start = datetime.now().strftime("%H:%M:%S")
        print(f"\nCycle {cycle} start at {cycle_start}")

        # Reload alert config each cycle so UI changes take effect without restart.
        alert_config = load_alert_config()

        try:
            divines_per_mirror = run_cycle(
                session, rate_limiter, output_csv, item_specs, cycle,
                divines_per_mirror, price_history, alert_config,
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"HTTP error during cycle {cycle}: status={status} error={exc}")
        except requests.RequestException as exc:
            print(f"Request error during cycle {cycle}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"Unexpected error during cycle {cycle}: {exc}")

        if cfg.max_cycles is not None and cycle >= cfg.max_cycles:
            print(f"Reached max-cycles ({cfg.max_cycles}). Stopping.")
            break

        sleep_seconds = seconds_until_next_tick(
            start_monotonic=start_monotonic,
            delay_seconds=cfg.poll_interval,
            now_monotonic=time.monotonic(),
        )
        # Console display: use local time for readability (not stored, console-only)
        next_run = datetime.now().timestamp() + sleep_seconds
        next_run_text = datetime.fromtimestamp(next_run).strftime("%H:%M:%S")
        print(f"Cycle {cycle} complete. Sleeping {int(sleep_seconds)}s until {next_run_text}")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
