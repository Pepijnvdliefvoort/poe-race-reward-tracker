from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .sale_inference_engine import (
    evaluate_listing_transition,
    listing_signals_from_fetch,
    non_instant_vanished_seller_accounts_for_online_probe,
)

from server.sales_discord_notify import send_estimated_sales_change_notification

from storage.service import StorageService, VariantSpec
from .db_export import DbExportConfig, maybe_export_db_to_discord

BASE_URL = "https://www.pathofexile.com/api/trade"
DEFAULT_LEAGUE = "Standard"
DEFAULT_ITEMS_FILE = "items.txt"
DEFAULT_CONFIG_FILE = "config.json"
LISTINGS_CACHE_MAX_ENTRIES = 1000
TOP_IDS_LIMIT = 20
# PoE trade search returns at most this many listing IDs in one response `result` array.
TRADE_SEARCH_RESULT_MAX = 100
# Default: fetch every ID the search returns for inference, up to TRADE_SEARCH_RESULT_MAX (more
# /fetch traffic for deep markets; reduces false "vanished" counts from a shallow top-N slice).
# Pricing and listing hover preview still use TOP_IDS_LIMIT only.
DEFAULT_INFERENCE_LISTINGS_FETCH_CAP = TRADE_SEARCH_RESULT_MAX
DIVINES_PER_MIRROR = 1650.0
EXALTS_PER_DIVINE = 60.0
MIN_RESALE_PROFIT_MIRRORS = 1.0
PRICE_EPSILON = 1e-6
CSV_HEADER: list[str] = []  # legacy (no longer written)

# Proactive safety margin over the natural allowed request pace.
RATE_LIMIT_SAFETY = 1.1
LOW_HEADROOM_THRESHOLD = 0.15
VERY_LOW_HEADROOM_THRESHOLD = 0.08
# Keep a reserve budget per window so this script leaves room for manual trade use.
RESERVE_RATIO = 0.20


def log_line(source: str, message: str) -> None:
    """Emit one structured log line.

    The structured logging tee adds the timestamp prefix; we add a consistent `[source]` tag.
    """
    src = source.strip() or "poller"
    msg = message.strip()
    print(f"[{src}] {msg}")


@dataclass
class Config:
    poll_interval: int
    max_cycles: int | None
    inference_fetch_cap: int


@dataclass
class AlertConfig:
    enabled: bool
    threshold_pct: float
    history_cycles: int
    min_total_results: int
    min_floor_listings: int
    floor_band_pct: float
    low_liquidity_extra_drop_pct: float
    cooldown_cycles: int
    webhook_url: str


@dataclass(frozen=True)
class ResaleOpportunity:
    buy_price: float
    next_market_price: float
    relist_price: float
    expected_profit: float


def load_discord_webhook_url_from_env() -> str:
    """Load Discord webhook from environment-managed secret."""
    for env_name in ("DISCORD_WEBHOOK_URL", "POE_DISCORD_WEBHOOK_URL"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


def load_discord_sales_webhook_url_from_env() -> tuple[str, bool]:
    """
    Webhook URL for estimated-sale signal notifications, and whether a dedicated
    sales URL was configured.

    If ``DISCORD_WEBHOOK_URL_SALES`` is unset, falls back to the same URL as
    price-drop alerts (``DISCORD_WEBHOOK_URL`` / ``POE_DISCORD_WEBHOOK_URL``)
    so est.-sales alerts still fire without a second secret.
    """
    sales = os.getenv("DISCORD_WEBHOOK_URL_SALES", "").strip()
    if sales:
        return sales, True
    return load_discord_webhook_url_from_env(), False


def load_discord_db_export_webhook_url_from_env() -> str:
    """
    Webhook URL for daily DB export uploads.

    This is intentionally separate from alerts, so you can point it to a private channel.
    """
    for env_name in ("DISCORD_WEBHOOK_URL_DB_EXPORT", "POE_DISCORD_WEBHOOK_URL_DB_EXPORT"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


def load_sales_discord_window_days(storage: StorageService) -> int:
    """Rolling window (days) for total est. sold in Discord; align with chart via app_config.market."""
    try:
        data = storage.get_config(key="market") or {}
        d = int(float(data.get("sales_discord_window_days", 90)))
    except Exception:
        d = 90
    return max(1, min(3650, d))


def load_inference_truncation_safe_margin_pct(storage: StorageService) -> float:
    """
    When the trade ladder is truncated (total_results > fetched IDs), we only treat a vanished row
    as a sale/removal if its price was clearly inside the observed slice, i.e. <= cutoff * (1 - margin).

    Config key: app_config.market.inference_truncation_safe_margin_pct (default 6.0)
    """
    try:
        data = storage.get_config(key="market") or {}
        raw = float(data.get("inference_truncation_safe_margin_pct", 6.0))
    except Exception:
        raw = 6.0
    if not math.isfinite(raw):
        raw = 6.0
    return max(0.0, min(50.0, float(raw)))


@dataclass(frozen=True)
class FlipConfig:
    """
    Config for whether a cheapest listing is "flip-worthy" enough to notify.

    Note: this poller evaluates opportunities on mirror-equivalent prices.
    """

    min_profit_mirrors: float
    misprice_override_enabled: bool
    misprice_override_max_buy_divines: float
    misprice_override_min_next_mirrors: float
    always_notify_if_buy_currency_exalted: bool


def load_flip_config(storage: StorageService) -> FlipConfig:
    """
    Config key: app_config.market.*

    Primary (clearer) keys:
    - notify_flip_min_profit_mirrors (default 1.0)
    - notify_always_if_cheap_enabled (default True)
    - notify_always_if_cheap_max_buy_divines (default 200.0)
    - notify_always_if_cheap_if_next_price_at_least_mirrors (default 0.5)
    - notify_always_if_buy_currency_exalted (default True)

    Legacy keys (still supported for backward compatibility):
    - flip_min_profit_mirrors
    - flip_misprice_override_enabled
    - flip_misprice_override_max_buy_divines
    - flip_misprice_override_min_next_mirrors
    """
    try:
        data = storage.get_config(key="market") or {}
    except Exception:
        data = {}

    def _f(key: str, default: float) -> float:
        try:
            v = float(data.get(key, default))
        except Exception:
            v = float(default)
        if not math.isfinite(v):
            v = float(default)
        return v

    def _b(key: str, default: bool) -> bool:
        try:
            return bool(data.get(key, default))
        except Exception:
            return bool(default)

    def _f2(primary: str, legacy: str, default: float) -> float:
        if primary in data:
            return _f(primary, default)
        return _f(legacy, default)

    def _b2(primary: str, legacy: str, default: bool) -> bool:
        if primary in data:
            return _b(primary, default)
        return _b(legacy, default)

    min_profit = max(0.0, _f2("notify_flip_min_profit_mirrors", "flip_min_profit_mirrors", MIN_RESALE_PROFIT_MIRRORS))
    override_enabled = _b2("notify_always_if_cheap_enabled", "flip_misprice_override_enabled", True)
    max_buy_div = max(0.0, _f2("notify_always_if_cheap_max_buy_divines", "flip_misprice_override_max_buy_divines", 200.0))
    min_next = max(
        0.0,
        _f2(
            "notify_always_if_cheap_if_next_price_at_least_mirrors",
            "flip_misprice_override_min_next_mirrors",
            0.5,
        ),
    )
    always_ex = _b("notify_always_if_buy_currency_exalted", True)
    return FlipConfig(
        min_profit_mirrors=float(min_profit),
        misprice_override_enabled=bool(override_enabled),
        misprice_override_max_buy_divines=float(max_buy_div),
        misprice_override_min_next_mirrors=float(min_next),
        always_notify_if_buy_currency_exalted=bool(always_ex),
    )


def load_trade_search_status_option(storage: StorageService) -> str:
    """
    Trade search `query.status.option`.

    The official trade site supports multiple "player status / trade method" modes. The meaning of
    these options has historically shifted and differs across trade experiences, so we keep it
    configurable via app_config.

    Config key: app_config.market.trade_status_option
    Common values: "online", "onlineleague", "available", "securable"
    """
    try:
        data = storage.get_config(key="market") or {}
        # Default: include both instant-buyout and in-person results (per trade site's "Any online items").
        raw = str(data.get("trade_status_option", "available")).strip().lower()
    except Exception:
        raw = "available"

    allowed = {"online", "onlineleague", "available", "securable"}
    if raw not in allowed:
        return "available"
    return raw


def load_alert_config() -> AlertConfig:
    webhook_url = load_discord_webhook_url_from_env()
    defaults = AlertConfig(
        enabled=False,
        threshold_pct=30.0,
        history_cycles=10,
        min_total_results=10,
        min_floor_listings=2,
        floor_band_pct=7.5,
        low_liquidity_extra_drop_pct=20.0,
        cooldown_cycles=6,
        webhook_url=webhook_url,
    )
    try:
        root_dir = Path(__file__).resolve().parents[1]
        storage = StorageService(root_dir=root_dir)
        data = storage.get_config(key="market")
        if data is None:
            # One-time bootstrap: allow a legacy config.json to seed DB config on first run.
            # After this, SQLite is the source of truth.
            config_path = root_dir / DEFAULT_CONFIG_FILE
            if config_path.exists():
                try:
                    with config_path.open("r", encoding="utf-8") as fh:
                        file_data = json.load(fh)
                    merged = {
                        "alert_enabled": bool(file_data.get("alert_enabled", defaults.enabled)),
                        "alert_threshold_pct": float(file_data.get("alert_threshold_pct", defaults.threshold_pct)),
                        "alert_history_cycles": max(1, int(file_data.get("alert_history_cycles", defaults.history_cycles))),
                        "alert_min_total_results": max(1, int(file_data.get("alert_min_total_results", defaults.min_total_results))),
                        "alert_min_floor_listings": max(1, int(file_data.get("alert_min_floor_listings", defaults.min_floor_listings))),
                        "alert_floor_band_pct": max(0.0, float(file_data.get("alert_floor_band_pct", defaults.floor_band_pct))),
                        "alert_low_liquidity_extra_drop_pct": max(
                            0.0,
                            float(
                                file_data.get(
                                    "alert_low_liquidity_extra_drop_pct",
                                    defaults.low_liquidity_extra_drop_pct,
                                )
                            ),
                        ),
                        "alert_cooldown_cycles": max(0, int(file_data.get("alert_cooldown_cycles", defaults.cooldown_cycles))),
                    }
                    storage.set_config(key="market", value=merged)
                    data = merged
                except Exception:
                    data = {}
            else:
                data = {}
        else:
            data = data or {}
        return AlertConfig(
            enabled=bool(data.get("alert_enabled", False)),
            threshold_pct=float(data.get("alert_threshold_pct", defaults.threshold_pct)),
            history_cycles=max(1, int(data.get("alert_history_cycles", defaults.history_cycles))),
            min_total_results=max(1, int(data.get("alert_min_total_results", defaults.min_total_results))),
            min_floor_listings=max(1, int(data.get("alert_min_floor_listings", defaults.min_floor_listings))),
            floor_band_pct=max(0.0, float(data.get("alert_floor_band_pct", defaults.floor_band_pct))),
            low_liquidity_extra_drop_pct=max(
                0.0, float(data.get("alert_low_liquidity_extra_drop_pct", defaults.low_liquidity_extra_drop_pct))
            ),
            cooldown_cycles=max(0, int(data.get("alert_cooldown_cycles", defaults.cooldown_cycles))),
            webhook_url=webhook_url,
        )
    except Exception:
        return defaults


def seed_price_history_from_db(storage: StorageService, history_cycles: int) -> dict[str, list[float]]:
    """Prime in-memory price history from SQLite (median_mirror per variant)."""
    history: dict[str, list[float]] = {}
    con = storage._db.connect()  # internal, OK for local polling
    try:
        rows = con.execute(
            """
            SELECT i.name AS item_name, v.mode AS mode, ip.median_mirror AS median_mirror
            FROM item_polls ip
            JOIN item_variants v ON v.id = ip.item_variant_id
            JOIN items i ON i.id = v.item_id
            WHERE ip.median_mirror IS NOT NULL
            ORDER BY ip.requested_at_utc ASC
            """
        ).fetchall()
        for r in rows:
            key = f"{str(r['item_name'])}::{str(r['mode'])}"
            history.setdefault(key, []).append(float(r["median_mirror"]))
        for k, v in list(history.items()):
            history[k] = v[-history_cycles:]
    finally:
        con.close()
    return history


def seed_price_alert_cooldown_from_db(
    storage: StorageService, variant_ids_by_key: dict[str, int]
) -> dict[str, tuple[int, float]]:
    """Restore price-drop alert cooldown from SQLite (same keys/cycles as ``run_cycle``)."""
    by_variant: dict[int, tuple[int, float]] = {}
    for vid, last_cycle, last_low in storage.load_price_alert_cooldown_rows():
        by_variant[int(vid)] = (int(last_cycle), float(last_low))
    state: dict[str, tuple[int, float]] = {}
    for key, vid in variant_ids_by_key.items():
        row = by_variant.get(int(vid))
        if row is not None:
            state[key] = row
    return state


def send_discord_alert(
    alert_session: requests.Session,
    item: ItemSpec,
    current_lowest: float,
    baseline: float,
    pct_drop: float,
    query_id: str,
    webhook_url: str,
    resale_opportunity: ResaleOpportunity | None = None,
    listing_summary: str | None = None,
    item_image_url: str | None = None,
) -> None:
    trade_url = f"https://www.pathofexile.com/trade/search/{DEFAULT_LEAGUE}/{query_id}"
    fields = [
        {
            "name": "Trade Link",
            "value": f"[View listing]({trade_url})",
            "inline": False,
        }
    ]
    if listing_summary:
        fields.append(
            {
                "name": "Top Listings",
                "value": listing_summary,
                "inline": False,
            }
        )
    if resale_opportunity is not None:
        fields.append(
            {
                "name": "Flip Window",
                "value": (
                    f"Buy at **{format_amount(resale_opportunity.buy_price)}** mirrors\n"
                    f"Next live listing: **{format_amount(resale_opportunity.next_market_price)}** mirrors\n"
                    f"Best relist: **{format_amount(resale_opportunity.relist_price)}** mirrors\n"
                    f"Estimated gross profit: **{format_amount(resale_opportunity.expected_profit)}** mirrors"
                ),
                "inline": False,
            }
        )

    embed = {
        "title": f"\N{BELL} Price Alert: {item.name}",
        "description": (
            f"Listed at **{format_amount(current_lowest)} mirrors** \u2014 "
            f"**{pct_drop:.1f}% below** baseline of {format_amount(baseline)} mirrors"
        ),
        "color": 0xFF6B35,
        "fields": fields,
    }
    if item_image_url:
        embed["thumbnail"] = {"url": item_image_url}
    log_line(
        "alert",
        (
            f"Price alert fired: {item.name} — {pct_drop:.1f}% below baseline "
            f"(low {format_amount(current_lowest)} vs {format_amount(baseline)} mirrors); "
            "sending Discord webhook"
        ),
    )
    try:
        resp = alert_session.post(
            webhook_url,
            json={"content": "@here", "embeds": [embed]},
            timeout=10.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log_line("warn", f"Failed to send Discord alert for {item.name}: {exc}")


@dataclass
class ItemSpec:
    name: str
    alternate_art: bool | None


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        prog="python -m poller",
        description=(
            "Continuously poll PoE trade prices for unique items listed in items.txt and append summaries to CSV."
        ),
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
    parser.add_argument(
        "--inference-cap",
        type=int,
        default=None,
        help=(
            f"Max search result IDs to fetch for sale inference per item (PoE caps at {TRADE_SEARCH_RESULT_MAX}; "
            f"0 disables). Default: app_config key 'inference_listings_fetch_cap' or {DEFAULT_INFERENCE_LISTINGS_FETCH_CAP}."
        ),
    )

    args = parser.parse_args()

    if args.poll_interval <= 0:
        raise SystemExit("--poll-interval must be > 0")
    if args.max_cycles is not None and args.max_cycles <= 0:
        raise SystemExit("--max-cycles must be > 0")
    if args.inference_cap is not None and args.inference_cap < 0:
        raise SystemExit("--inference-cap must be >= 0")

    inference_cap = load_inference_fetch_cap(args.inference_cap)
    return Config(
        poll_interval=args.poll_interval,
        max_cycles=args.max_cycles,
        inference_fetch_cap=inference_cap,
    )


def load_inference_fetch_cap(cli_override: int | None) -> int:
    def _clamp(v: int) -> int:
        v = max(0, int(v))
        if v == 0:
            return 0
        return min(TRADE_SEARCH_RESULT_MAX, v)

    if cli_override is not None:
        return _clamp(cli_override)

    try:
        root_dir = Path(__file__).resolve().parents[1]
        storage = StorageService(root_dir=root_dir)
        cfg = storage.get_config(key="market") or {}
        raw = cfg.get("inference_listings_fetch_cap", DEFAULT_INFERENCE_LISTINGS_FETCH_CAP)
        return _clamp(int(raw))
    except Exception:
        return DEFAULT_INFERENCE_LISTINGS_FETCH_CAP


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
        request_label: str = "",
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

        self._log_live_status(windows, wait_seconds, status_code, request_label=request_label)

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

    def _log_live_status(
        self,
        windows: list[RateWindowState],
        next_wait: float,
        status_code: int,
        request_label: str = "",
    ) -> None:
        parts: list[str] = []
        for window in windows:
            remaining = max(0, window.max_requests - window.used_requests)
            parts.append(
                f"{window.source} {window.used_requests}/{window.max_requests} "
                f"(rem={remaining}, retry={window.retry_after_seconds}s)"
            )

        summary = " | ".join(parts)
        label = request_label.strip()
        source = label or "rate"
        log_line(source, f"status={status_code} {summary}; next in ~{next_wait:.2f}s")


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


def build_search_payload(
    item: ItemSpec,
    price_currency: str | None = None,
    *,
    status_option_override: str | None = None,
) -> dict[str, Any]:
    # Configurable so users can choose whether to include/exclude certain trade modes.
    # Default: online-only (excludes offline sellers).
    if status_option_override is not None:
        status_option = str(status_option_override).strip().lower()
    else:
        try:
            root_dir = Path(__file__).resolve().parents[1]
            storage = StorageService(root_dir=root_dir)
            status_option = load_trade_search_status_option(storage)
        except Exception:
            # Fail-safe: prefer "available" so offline sellers don't look like sales.
            status_option = "available"

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
        "status": {"option": status_option},
        "name": name_option,
        "stats": [{"type": "and", "filters": [], "disabled": False}],
        "filters": {
            "misc_filters": {
                "filters": misc_filters,
                "disabled": False,
            }
        },
    }

    if price_currency is not None:
        normalized = price_currency.strip().lower()
        if normalized not in {"divine"}:
            raise ValueError(f"Unsupported price currency filter: {price_currency}")
        # Trade site quirk: "price" filter restricts the listed currency (e.g. divines only).
        # This is used as a fallback when the sorted ladder starts with "1 mirror" listings.
        query["filters"]["trade_filters"] = {
            "filters": {
                "price": {"option": normalized},
            }
        }

    return {
        "query": query,
        "sort": {"price": "asc"},
    }


def search_item(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    item: ItemSpec,
    price_currency: str | None = None,
    *,
    status_option_override: str | None = None,
) -> tuple[str, list[str], int]:
    url = f"{BASE_URL}/search/{DEFAULT_LEAGUE}"
    payload = build_search_payload(
        item,
        price_currency=price_currency,
        status_option_override=status_option_override,
    )

    rate_limiter.wait_before_request()
    response = session.post(url, data=json.dumps(payload), timeout=30.0)
    label = "trade.search" if price_currency is None else f"trade.search[{price_currency}]"
    rate_limiter.update_from_response(response.headers, response.status_code, request_label=label)
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


def build_account_search_payload(account_name: str) -> dict[str, Any]:
    """
    Minimal trade search JSON: filter by seller account (for "is the seller online in this league?"
    probe after a non-instant listing vanishes).

    Important: PoE trade "online" status is not strictly tied to a specific league. Using
    ``onlineleague`` prevents false positives where the account is online elsewhere (e.g. playing a
    different league) while still having listings in Standard.
    """
    acct = (account_name or "").strip()
    return {
        "query": {
            "status": {"option": "onlineleague"},
            "stats": [{"type": "and", "filters": []}],
            "filters": {
                "trade_filters": {
                    "filters": {
                        "account": {"input": acct},
                    }
                }
            },
        },
        "sort": {"price": "asc"},
    }


def search_trade_by_account(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    *,
    league: str,
    account_name: str,
) -> tuple[str | None, list[str], int]:
    url = f"{BASE_URL}/search/{league}"
    payload = build_account_search_payload(account_name)

    rate_limiter.wait_before_request()
    response = session.post(url, data=json.dumps(payload), timeout=30.0)
    rate_limiter.update_from_response(
        response.headers, response.status_code, request_label="trade.search[account]"
    )
    response.raise_for_status()

    data = response.json()
    query_id = data.get("id")
    result_ids = data.get("result", [])
    total = data.get("total", 0)

    if not isinstance(query_id, str) or not query_id:
        return None, [], 0
    if not isinstance(result_ids, list):
        return None, [], 0
    if not isinstance(total, int):
        total = len(result_ids)

    ids = [x for x in result_ids if isinstance(x, str)]
    return query_id, ids, int(total)


def probe_seller_online_via_account_search(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    *,
    league: str,
    account_name: str,
) -> bool | None:
    """
    PoE trade: account-filter search, then GET /fetch for the first result id with ``?query=``.

    Returns whether ``listing.account.online`` is true on that listing. Uses the same rate limiter
    as the rest of the poller (one search + one fetch per seller).

    Returns:
        True/False when the probe completes successfully.
        None on HTTP errors / malformed responses (caller falls back to snapshot ``sellerOnline``).

    If the account search returns no hits, returns False (cannot observe online; treat as not online
    for rule 4b).
    """
    acct = (account_name or "").strip()
    if not acct or acct == "unknown":
        return None

    try:
        query_id, result_ids, total = search_trade_by_account(
            session, rate_limiter, league=league, account_name=acct
        )
    except Exception:
        return None

    if not query_id or total <= 0 or not result_ids:
        return False

    try:
        rows = _fetch_listing_entries_batched(session, rate_limiter, query_id, [result_ids[0]])
    except Exception:
        return None

    if not rows:
        return False
    entry = rows[0]
    listing = entry.get("listing") if isinstance(entry, dict) else None
    account = listing.get("account") if isinstance(listing, dict) else None
    if isinstance(account, dict) and "online" in account:
        return bool(account.get("online"))
    return False


def find_first_priced_listing(listings: list[dict[str, Any]]) -> tuple[str, float] | None:
    """Return (currency, amount) for the first listing with a supported price."""
    for entry in listings:
        listing = entry.get("listing") if isinstance(entry, dict) else None
        price = listing.get("price") if isinstance(listing, dict) else None
        if not isinstance(price, dict):
            continue
        normalized = normalize_price_currency(price)
        if normalized is not None:
            return normalized
    return None


def _fetch_listing_entries_batched(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    query_id: str,
    ids: list[str],
) -> list[dict[str, Any]]:
    """GET /fetch in batches of 10 (API limit per request)."""
    if not ids:
        return []

    all_results: list[dict[str, Any]] = []
    url_base = f"{BASE_URL}/fetch/"
    batch_size = 10
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        ids_joined = ",".join(batch)
        url = f"{url_base}{ids_joined}"
        rate_limiter.wait_before_request()
        response = session.get(url, params={"query": query_id}, timeout=30.0)
        rate_limiter.update_from_response(response.headers, response.status_code, request_label="trade.fetch")
        response.raise_for_status()

        data = response.json()
        result = data.get("result", [])
        if not isinstance(result, list):
            raise RuntimeError("Fetch endpoint returned malformed result data")
        all_results.extend(result)

    return all_results


def fetch_top_listings(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    query_id: str,
    result_ids: list[str],
) -> list[dict[str, Any]]:
    # The trade fetch endpoint has a practical per-request ID limit.
    # We fetch up to TOP_IDS_LIMIT in small batches to keep summaries robust
    # (median/high) while still showing only a small preview in the UI.
    top_ids = [x for x in result_ids[:TOP_IDS_LIMIT] if isinstance(x, str)]
    return _fetch_listing_entries_batched(session, rate_limiter, query_id, top_ids)


def fetch_listings_for_inference(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    query_id: str,
    result_ids: list[str],
    cap: int,
) -> list[dict[str, Any]]:
    """All listing payloads returned by search (same order), up to `cap`."""
    if cap <= 0:
        return []
    ids = [x for x in result_ids if isinstance(x, str)][:cap]
    return _fetch_listing_entries_batched(session, rate_limiter, query_id, ids)


def _median_absolute_deviation(values: list[float], median_value: float) -> float:
    deviations = [abs(v - median_value) for v in values]
    if not deviations:
        return 0.0
    return float(statistics.median(deviations))


def filter_prices_mad(prices: list[float], threshold_sigma: float = 6.0) -> list[float]:
    """Filter out extreme prices using a MAD-based robust z-score cutoff.

    This intentionally removes obvious "show-off" prices (very large) and also
    discards extreme low anomalies that are far from the market cluster.
    """
    cleaned = [p for p in prices if isinstance(p, (int, float)) and math.isfinite(p) and p > 0]
    if len(cleaned) < 5:
        return cleaned

    med = float(statistics.median(cleaned))
    mad = _median_absolute_deviation(cleaned, med)
    if mad <= 0:
        return cleaned

    # Consistent estimator for normal distributions.
    sigma = 1.4826 * mad
    cutoff = threshold_sigma * sigma
    return [p for p in cleaned if abs(p - med) <= cutoff]


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
    if normalized_currency in {"exalted", "exalt", "exa", "exalted orb", "exalted orbs"}:
        return "exalted", float(amount)

    return None


def find_cheapest_supported_listing_price(
    listings: list[dict[str, Any]],
    *,
    divines_per_mirror: float,
) -> tuple[str, float, float] | None:
    """
    Return (currency, amount, mirror_equiv) for the cheapest listing among `listings`
    where currency is one of: mirror|divine|exalted.
    """
    dpm = (
        float(divines_per_mirror)
        if isinstance(divines_per_mirror, (int, float))
        and math.isfinite(float(divines_per_mirror))
        and float(divines_per_mirror) > 0
        else DIVINES_PER_MIRROR
    )

    best: tuple[str, float, float] | None = None
    best_m: float | None = None
    for entry in listings:
        listing = entry.get("listing") if isinstance(entry, dict) else None
        price = listing.get("price") if isinstance(listing, dict) else None
        if not isinstance(price, dict):
            continue
        normalized = normalize_price_currency(price)
        if normalized is None:
            continue
        currency, amount = normalized
        mirror_equiv = to_mirror_equivalent(float(amount), str(currency), dpm)
        if not (isinstance(mirror_equiv, (int, float)) and math.isfinite(float(mirror_equiv)) and mirror_equiv > 0):
            continue
        if best_m is None or mirror_equiv < best_m:
            best_m = float(mirror_equiv)
            best = (str(currency), float(amount), float(mirror_equiv))
    return best


def extract_listing_prices(listings: list[dict[str, Any]]) -> tuple[list[float], list[float], int]:
    mirror_prices: list[float] = []
    divine_prices: list[float] = []
    exalted_prices: list[float] = []
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
        elif currency == "exalted":
            exalted_prices.append(amount)

    # Treat exalts as supported by converting them into divines using a fixed ratio.
    divine_from_exalts = [p / EXALTS_PER_DIVINE for p in exalted_prices]
    return mirror_prices, divine_prices + divine_from_exalts, unsupported_count


def format_listing_summary_price(currency: str, amount: float, divines_per_mirror: float) -> str:
    formatted_amount = format_amount(amount)
    if currency == "mirror":
        unit = "mirror" if amount == 1 else "mirrors"
        return f"{formatted_amount} {unit}"

    if currency == "divine":
        mirror_equivalent = to_mirror_equivalent(amount, currency, divines_per_mirror)
        return f"{formatted_amount} divines (~{format_amount(mirror_equivalent)} mirrors)"

    if currency == "exalted":
        div_amount = amount / EXALTS_PER_DIVINE
        mirror_equivalent = to_mirror_equivalent(div_amount, "divine", divines_per_mirror)
        return f"{formatted_amount} exalts (~{format_amount(mirror_equivalent)} mirrors)"

    # Safe fallback (shouldn't happen if normalize_price_currency is used consistently).
    mirror_equivalent = to_mirror_equivalent(amount, currency, divines_per_mirror)
    return f"{formatted_amount} {currency} (~{format_amount(mirror_equivalent)} mirrors)"


def extract_listing_seller_name(entry: dict[str, Any]) -> str:
    listing = entry.get("listing") if isinstance(entry, dict) else None
    account = listing.get("account") if isinstance(listing, dict) else None
    if isinstance(account, dict):
        account_name = account.get("name")
        if isinstance(account_name, str) and account_name:
            return account_name

        character_name = account.get("lastCharacterName")
        if isinstance(character_name, str) and character_name:
            return character_name

    return "unknown seller"


def format_relative_time(indexed_timestamp: str) -> str:
    """Format an ISO 8601 timestamp as relative time (e.g., '3 months ago')."""
    try:
        posted_time = datetime.fromisoformat(indexed_timestamp.replace("Z", "+00:00"))
        if posted_time.tzinfo is None:
            posted_time = posted_time.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        diff = now - posted_time
        
        # Calculate time units
        total_seconds = diff.total_seconds()
        
        if total_seconds < 60:
            return "just now"
        
        minutes = total_seconds / 60
        if minutes < 60:
            m = int(minutes)
            return f"{m} minute{'s' if m != 1 else ''} ago"
        
        hours = minutes / 60
        if hours < 24:
            h = int(hours)
            return f"{h} hour{'s' if h != 1 else ''} ago"
        
        days = hours / 24
        if days < 7:
            d = int(days)
            return f"{d} day{'s' if d != 1 else ''} ago"
        
        weeks = days / 7
        if weeks < 4:
            w = int(weeks)
            return f"{w} week{'s' if w != 1 else ''} ago"
        
        months = days / 30.44
        if months < 12:
            mo = int(months)
            return f"{mo} month{'s' if mo != 1 else ''} ago"
        
        years = months / 12
        y = int(years)
        return f"{y} year{'s' if y != 1 else ''} ago"
    except (ValueError, AttributeError):
        return "unknown"


def extract_listing_posted_time(entry: dict[str, Any]) -> str:
    """Extract and format the posted time from a listing entry."""
    listing = entry.get("listing") if isinstance(entry, dict) else None
    indexed = listing.get("indexed") if isinstance(listing, dict) else None
    if isinstance(indexed, str):
        return format_relative_time(indexed)
    return "unknown"


def _format_listing_preview_price(price: dict[str, Any] | None) -> tuple[str, float | None, str | None]:
    if not isinstance(price, dict):
        return "No listed price", None, None

    amount = price.get("amount")
    currency = price.get("currency")
    if not isinstance(amount, (int, float)) or not isinstance(currency, str) or not currency.strip():
        return "No listed price", None, None

    normalized_currency = currency.strip().lower()
    amount_float = float(amount)
    return f"{format_amount(amount_float)} {normalized_currency}", amount_float, normalized_currency


def build_listing_preview_entries(
    listings: list[dict[str, Any]],
    max_entries: int | None = None,
) -> list[dict[str, Any]]:
    preview_rows: list[dict[str, Any]] = []

    for entry in listings:
        if max_entries is not None and len(preview_rows) >= max_entries:
            break

        listing = entry.get("listing") if isinstance(entry, dict) else None
        if not isinstance(listing, dict):
            continue

        price = listing.get("price") if isinstance(listing.get("price"), dict) else None
        price_text, amount, currency = _format_listing_preview_price(price)

        note = str(listing.get("note") or "")
        buyout_type = str(price.get("type") or "") if isinstance(price, dict) else ""
        is_instant_buyout = "b/o" in buyout_type.lower() or "~b/o" in note.lower()

        indexed = listing.get("indexed") if isinstance(listing.get("indexed"), str) else None

        preview_rows.append(
            {
                "priceText": price_text,
                "amount": amount,
                "currency": currency,
                "isInstantBuyout": is_instant_buyout,
                "sellerName": extract_listing_seller_name(entry),
                "posted": extract_listing_posted_time(entry),
                "indexed": indexed,
            }
        )

    return preview_rows


def write_listings_cache(path: Path, by_query_id: dict[str, dict[str, Any]]) -> None:
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "league": DEFAULT_LEAGUE,
        "byQueryId": by_query_id,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True)
    tmp_path.replace(path)


def load_listings_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}

    by_query = payload.get("byQueryId") if isinstance(payload, dict) else None
    if not isinstance(by_query, dict):
        return {}

    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in by_query.items():
        if isinstance(key, str) and key and isinstance(value, dict):
            cleaned[key] = value
    return cleaned


def prune_listings_cache(by_query_id: dict[str, dict[str, Any]], max_entries: int = LISTINGS_CACHE_MAX_ENTRIES) -> dict[str, dict[str, Any]]:
    if len(by_query_id) <= max_entries:
        return by_query_id

    def updated_key(item: tuple[str, dict[str, Any]]) -> str:
        value = item[1]
        updated_at = value.get("updatedAt") if isinstance(value, dict) else None
        return updated_at if isinstance(updated_at, str) else ""

    recent_items = sorted(by_query_id.items(), key=updated_key, reverse=True)[:max_entries]
    return dict(recent_items)


def build_top_listing_summary(
    listings: list[dict[str, Any]],
    divines_per_mirror: float,
    max_entries: int = 5,
) -> str | None:
    summary_lines: list[str] = []

    for entry in listings:
        listing = entry.get("listing") if isinstance(entry, dict) else None
        price = listing.get("price") if isinstance(listing, dict) else None
        if not isinstance(price, dict):
            continue

        normalized = normalize_price_currency(price)
        if normalized is None:
            continue

        currency, amount = normalized
        seller_name = extract_listing_seller_name(entry)
        posted_time = extract_listing_posted_time(entry)
        price_text = format_listing_summary_price(currency, amount, divines_per_mirror)
        summary_lines.append(f"{len(summary_lines) + 1}. {price_text} - {seller_name} - {posted_time}")

        if len(summary_lines) >= max_entries:
            break

    if not summary_lines:
        return None

    return "\n".join(summary_lines)


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

    cleaned = [p for p in prices if isinstance(p, (int, float)) and math.isfinite(p) and p > 0]
    if not cleaned:
        return None, None, None

    # Keep floor from raw (alerts/flip logic depends on cheapest listing),
    # but compute the chart median/high from a robust "core" set so
    # one-off vanity prices don't dominate the summary range.
    low = min(cleaned)

    core = filter_prices_mad(cleaned, threshold_sigma=6.0)
    if not core:
        core = cleaned

    high = max(core)
    median = float(statistics.median(core))
    return low, high, median


def count_prices_within_band(prices: list[float], anchor: float, band_pct: float) -> int:
    """Count listings priced near the floor to avoid one-off low outliers triggering alerts."""
    if anchor <= 0:
        return 0
    ceiling = anchor * (1.0 + (band_pct / 100.0))
    return sum(1 for price in prices if price <= ceiling)


def find_best_relist_price(limit_price: float, *, divines_per_mirror: float) -> float | None:
    """Return the highest allowed relist price that still undercuts the next live listing."""
    if limit_price <= 0:
        return None

    target_limit = limit_price - PRICE_EPSILON
    if target_limit <= 0:
        return None

    # For very high-priced items, a coarse mirror step is fine.
    # For "around 1 mirror" markets, we need a fine step (effectively: 1 divine in mirror units)
    # otherwise undercutting a "1 mirror" next listing yields 0 and blocks all flip notifications.
    if limit_price >= 5.0:
        step_mirrors = 1.0
    else:
        dpm = (
            float(divines_per_mirror)
            if isinstance(divines_per_mirror, (int, float))
            and math.isfinite(float(divines_per_mirror))
            and float(divines_per_mirror) > 0
            else DIVINES_PER_MIRROR
        )
        step_mirrors = max(1.0 / dpm, 0.000001)

    units = math.floor(target_limit / step_mirrors)
    candidate = round(units * step_mirrors, 6)
    if candidate <= 0 or candidate >= limit_price:
        return None
    return candidate


def find_resale_opportunity(
    prices: list[float],
    *,
    divines_per_mirror: float,
    cfg: FlipConfig,
    cheapest_raw: tuple[str, float, float] | None = None,
) -> ResaleOpportunity | None:
    """Check whether the cheapest listing can be flipped while staying cheapest on the live ladder."""
    sorted_prices = sorted(price for price in prices if price > 0)
    if len(sorted_prices) < 2:
        return None

    buy_price = sorted_prices[0]
    next_market_price = sorted_prices[1]
    relist_price = find_best_relist_price(next_market_price, divines_per_mirror=float(divines_per_mirror))
    if relist_price is None or relist_price <= buy_price + PRICE_EPSILON:
        return None

    expected_profit = relist_price - buy_price
    min_profit = float(cfg.min_profit_mirrors)

    # Misprice override:
    # If the market is mirror-tier but the cheapest ask is "a few ex / very low div", still notify.
    override = False
    if cfg.misprice_override_enabled:
        dpm = (
            float(divines_per_mirror)
            if isinstance(divines_per_mirror, (int, float))
            and math.isfinite(float(divines_per_mirror))
            and float(divines_per_mirror) > 0
            else DIVINES_PER_MIRROR
        )
        buy_divines = buy_price * dpm
        if next_market_price >= float(cfg.misprice_override_min_next_mirrors) and buy_divines <= float(
            cfg.misprice_override_max_buy_divines
        ):
            override = True

    if (
        not override
        and cfg.always_notify_if_buy_currency_exalted
        and cheapest_raw is not None
        and str(cheapest_raw[0]) == "exalted"
        and next_market_price >= float(cfg.misprice_override_min_next_mirrors)
    ):
        # Treat "listed in exalts" as a strong misprice signal; bypass the min-profit gate.
        override = True

    if not override and expected_profit + PRICE_EPSILON < min_profit:
        return None

    return ResaleOpportunity(
        buy_price=buy_price,
        next_market_price=next_market_price,
        relist_price=relist_price,
        expected_profit=expected_profit,
    )


def format_amount(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def to_mirror_equivalent(amount: float, currency: str, divines_per_mirror: float = DIVINES_PER_MIRROR) -> float:
    if currency == "mirror":
        return amount
    if currency == "divine":
        return amount / divines_per_mirror
    if currency == "exalted":
        return (amount / EXALTS_PER_DIVINE) / divines_per_mirror
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
    rate_limiter.update_from_response(response.headers, response.status_code, request_label="trade.exchange")
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
        log_line("trade.exchange", "Mirror of Kalandra: unexpected result format from exchange endpoint.")
        return None

    if not entries:
        log_line("trade.exchange", "Mirror of Kalandra: no exchange listings found.")
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
        log_line("trade.exchange", "Mirror of Kalandra: no valid divine-priced exchange listings in top listings.")
        return None

    median_price = statistics.median(divine_prices)
    log_line(
        "trade.exchange",
        f"Mirror of Kalandra: top-{len(divine_prices)} median = {format_amount(median_price)} divine (ratio updated)",
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
        log_line(
            "warn",
            f"Existing CSV header did not match current format. Backed up old file to {backup_path}.",
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
    inference_confirmed_transfer: int,
    inference_likely_instant_sale: int,
    inference_relist_same_seller: int,
    inference_non_instant_removed: int,
    inference_reprice_same_seller: int,
    inference_multi_seller_same_fingerprint: int,
    inference_new_listing_rows: int,
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
                str(inference_confirmed_transfer),
                str(inference_likely_instant_sale),
                str(inference_relist_same_seller),
                str(inference_non_instant_removed),
                str(inference_reprice_same_seller),
                str(inference_multi_seller_same_fingerprint),
                str(inference_new_listing_rows),
            ]
        )


def run_cycle(
    session: requests.Session,
    rate_limiter: AdaptiveRateLimiter,
    specs: list[ItemSpec],
    cycle: int,
    storage: StorageService,
    variant_ids_by_key: dict[str, int],
    run_started_at_utc: str,
    divines_per_mirror: float = DIVINES_PER_MIRROR,
    price_history: dict[str, list[float]] | None = None,
    alert_state: dict[str, tuple[int, float]] | None = None,
    alert_config: AlertConfig | None = None,
    inference_fetch_cap: int = DEFAULT_INFERENCE_LISTINGS_FETCH_CAP,
    sales_webhook_url: str = "",
    sales_window_days: int = 90,
) -> float:
    """Run one poll cycle; returns the updated divines-per-mirror ratio."""
    # SQLite is the source of truth (no CSV or listings cache file).

    # Fetch Mirror of Kalandra price first so the live ratio is ready before item processing.
    new_mirror_ratio = fetch_mirror_divine_median(session, rate_limiter)
    if new_mirror_ratio is not None:
        divines_per_mirror = new_mirror_ratio

    total_items = len(specs)
    max_mode_label_len = max(len(item_mode_label(spec)) for spec in specs)
    max_name_len = max(len(spec.name) for spec in specs)

    for index, item in enumerate(specs, start=1):
        request_timestamp_utc = datetime.now(timezone.utc).isoformat()
        item_key = f"{item.name}::{item_mode_token(item)}"
        variant_id = int(variant_ids_by_key.get(item_key) or 0)

        # For the first-ever poll of an item variant, force "available" so offline listings
        # don't look like vanished/sold listings in the inference engine.
        status_override = "available" if not storage.has_any_item_polls(variant_id=variant_id) else None

        query_id, result_ids, total_results = search_item(
            session,
            rate_limiter,
            item,
            status_option_override=status_override,
        )
        listings = fetch_top_listings(session, rate_limiter, query_id, result_ids)

        result_id_count = len([x for x in result_ids if isinstance(x, str)])
        if inference_fetch_cap <= 0:
            effective_inference_cap = 0
        else:
            effective_inference_cap = min(
                TRADE_SEARCH_RESULT_MAX,
                result_id_count,
                inference_fetch_cap,
            )

        listings_inference = fetch_listings_for_inference(
            session,
            rate_limiter,
            query_id,
            result_ids,
            cap=effective_inference_cap,
        )
        cheapest_icon_url = find_cheapest_listing_icon(listings, divines_per_mirror)
        top_listing_summary = build_top_listing_summary(listings, divines_per_mirror)
        listing_preview_rows = build_listing_preview_entries(listings)
        inference_signals = listing_signals_from_fetch(listings_inference, divines_per_mirror)
        for idx, row in enumerate(listing_preview_rows):
            if idx < len(inference_signals):
                row["fingerprint"] = inference_signals[idx]["fingerprint"]

        prev_signals, pending_inst, pending_on = ([], [], [])
        if variant_id:
            prev_signals, pending_inst, pending_on = storage.load_inference_state(variant_id=variant_id)
        truncation_margin_pct = load_inference_truncation_safe_margin_pct(storage)
        snap_trunc = bool(effective_inference_cap > 0 and total_results > effective_inference_cap)
        trunc_cutoff = max(
            (
                float(s.get("mirrorEquiv"))
                for s in (inference_signals or [])
                if isinstance(s, dict)
                and isinstance(s.get("mirrorEquiv"), (int, float))
                and math.isfinite(float(s.get("mirrorEquiv")))
            ),
            default=None,
        )

        seller_online_probe: dict[str, bool] = {}
        probe_accounts = non_instant_vanished_seller_accounts_for_online_probe(
            prev_signals,
            inference_signals,
            snapshot_truncated=snap_trunc,
            truncation_cutoff_mirror=trunc_cutoff,
            truncation_safe_margin_pct=float(truncation_margin_pct),
        )
        for acct in probe_accounts:
            online_guess = probe_seller_online_via_account_search(
                session, rate_limiter, league=DEFAULT_LEAGUE, account_name=acct
            )
            if online_guess is not None:
                seller_online_probe[acct] = online_guess

        inf, new_pending_inst, new_pending_on, curr_signals = evaluate_listing_transition(
            item_key=item_key,
            cycle=cycle,
            prev_signals=prev_signals,
            curr_signals=inference_signals,
            pending_instant=pending_inst,
            pending_online=pending_on,
            seller_online_probe=seller_online_probe,
            snapshot_truncated=snap_trunc,
            truncation_cutoff_mirror=trunc_cutoff,
            truncation_safe_margin_pct=float(truncation_margin_pct),
        )
        (
            xfer,
            instant,
            online_ni,
            relist,
            nib,
            repr_seller,
            multi_fp,
            new_rows,
        ) = inf.to_csv_tuple()

        # Listing previews + inference events are persisted in SQLite (no JSON cache file).

        raw_mirror_prices, divine_prices, unsupported_count = extract_listing_prices(listings)

        # Convert all divine-priced listings (from the regular ladder) to mirror equivalents.
        converted = [p / divines_per_mirror for p in divine_prices]

        # Fallback: If the trade ladder begins with a "1 mirror" listing, it may be hiding cheaper
        # divine-priced listings due to the site's internal conversion rate. In that case, run an
        # additional search restricted to divines-only, then convert those prices into mirrors.
        divine_only_converted: list[float] = []
        first_price = find_first_priced_listing(listings)
        if first_price is not None:
            first_currency, first_amount = first_price
            if first_currency == "mirror" and abs(first_amount - 1.0) <= 1e-9:
                divine_query_id, divine_only_ids, _ = search_item(
                    session,
                    rate_limiter,
                    item,
                    price_currency="divine",
                    status_option_override=status_override,
                )
                divine_only_listings = fetch_top_listings(
                    session, rate_limiter, divine_query_id, divine_only_ids
                )
                _, divine_only_prices_raw, _ = extract_listing_prices(divine_only_listings)
                divine_only_converted = [p / divines_per_mirror for p in divine_only_prices_raw]

        mirror_prices = raw_mirror_prices + converted + divine_only_converted

        low_mirror, high_mirror, median_mirror = summarize_prices(mirror_prices)
        cheapest_raw = find_cheapest_supported_listing_price(listings, divines_per_mirror=float(divines_per_mirror))
        resale_opportunity = find_resale_opportunity(
            mirror_prices,
            divines_per_mirror=float(divines_per_mirror),
            cfg=load_flip_config(storage),
            cheapest_raw=cheapest_raw,
        )
        used_results = len(mirror_prices)

        low_divine, high_divine, median_divine = summarize_prices(divine_only_converted)

        # Poll summary is persisted in SQLite (no CSV file).

        if variant_id:
            storage.write_poll_result(
                cycle_number=cycle,
                league=DEFAULT_LEAGUE,
                run_started_at_utc=run_started_at_utc,
                requested_at_utc=request_timestamp_utc,
                divines_per_mirror=divines_per_mirror,
                top_ids_limit=TOP_IDS_LIMIT,
                inference_fetch_cap=effective_inference_cap,
                variant_id=variant_id,
                query_id=query_id,
                total_results=total_results,
                used_results=used_results,
                unsupported_price_count=unsupported_count,
                mirror_count=len(mirror_prices),
                lowest_mirror=low_mirror,
                median_mirror=median_mirror,
                highest_mirror=high_mirror,
                divine_count=len(divine_only_converted),
                lowest_divine=low_divine,
                median_divine=median_divine,
                highest_divine=high_divine,
                inference_counts={
                    "confirmedTransfer": inf.confirmed_transfer,
                    "likelyInstantSale": inf.likely_instant_sale,
                    "likelyNonInstantOnline": inf.likely_non_instant_online,
                    "relistSameSeller": inf.relist_same_seller,
                    "nonInstantRemoved": inf.non_instant_removed,
                    "repriceSameSeller": inf.reprice_same_seller,
                    "multiSellerSameFingerprint": inf.multi_seller_same_fingerprint,
                    "newListingRows": inf.new_listing_rows,
                },
                fetched_for_inference=len(listings_inference),
                listing_preview_rows=listing_preview_rows,
                inference_events=inf.events,
                inference_state=(curr_signals, new_pending_inst, new_pending_on),
            )

            cycle_est = (
                int(inf.confirmed_transfer)
                + int(inf.likely_instant_sale)
                + int(inf.likely_non_instant_online)
            )
            if sales_webhook_url and cycle_est != 0:
                try:
                    since = (datetime.now(timezone.utc) - timedelta(days=int(sales_window_days))).isoformat()
                    total_est = storage.sum_estimated_sales_since(variant_id=variant_id, since_utc_iso=since)
                    log_line(
                        "alert",
                        (
                            f"Est. sales alert fired: {item.name} delta={cycle_est} "
                            f"(xfer={inf.confirmed_transfer} instant={inf.likely_instant_sale} "
                            f"non_inst_online={inf.likely_non_instant_online}) "
                            f"total~={total_est} ({sales_window_days}d); sending Discord webhook"
                        ),
                    )
                    send_estimated_sales_change_notification(
                        session,
                        webhook_url=sales_webhook_url,
                        item_name=item.name,
                        item_image_url=cheapest_icon_url,
                        cycle_delta=cycle_est,
                        total_in_window=total_est,
                        window_days=int(sales_window_days),
                        confirmed_transfer=int(inf.confirmed_transfer),
                        likely_instant_sale=int(inf.likely_instant_sale),
                        likely_non_instant_online=int(inf.likely_non_instant_online),
                        divines_per_mirror=divines_per_mirror,
                        inference_events=inf.events,
                    )
                except Exception as exc:  # noqa: BLE001
                    log_line("warn", f"Failed sales Discord webhook for {item.name}: {exc}")

        cheapest_text = "n/a" if low_mirror is None else f"{format_amount(low_mirror)} mirrors"

        # --- Alert check ---
        if price_history is None or alert_state is None or alert_config is None:
            pass  # Skip alert processing if context missing
        elif not alert_config.enabled:
            pass  # Skip alert processing if disabled
        else:
            history = price_history.setdefault(item_key, [])

            if median_mirror is not None and low_mirror is not None and len(history) >= alert_config.history_cycles:
                baseline = statistics.median(history[-alert_config.history_cycles:])
                if baseline > 0:
                    pct_drop = (baseline - low_mirror) / baseline * 100
                    floor_depth = count_prices_within_band(
                        mirror_prices,
                        low_mirror,
                        alert_config.floor_band_pct,
                    )
                    required_drop_pct = alert_config.threshold_pct
                    if total_results < alert_config.min_total_results:
                        # Thin markets can still alert, but require a larger discount to reduce noise.
                        required_drop_pct += alert_config.low_liquidity_extra_drop_pct
                        meets_floor_depth = floor_depth >= 1
                    else:
                        meets_floor_depth = floor_depth >= alert_config.min_floor_listings

                    if (
                        pct_drop >= required_drop_pct
                        and alert_config.webhook_url
                        and meets_floor_depth
                        and resale_opportunity is not None
                    ):
                        last_alert = alert_state.get(item_key)
                        suppress_for_cooldown = False
                        if last_alert is not None and alert_config.cooldown_cycles > 0:
                            last_cycle, last_price = last_alert
                            same_price = abs(last_price - low_mirror) <= max(0.01, last_price * 0.01)
                            if same_price and (cycle - last_cycle) < alert_config.cooldown_cycles:
                                suppress_for_cooldown = True

                        if suppress_for_cooldown:
                            pass
                        else:
                            send_discord_alert(
                                session,
                                item,
                                low_mirror,
                                baseline,
                                pct_drop,
                                query_id,
                                alert_config.webhook_url,
                                resale_opportunity=resale_opportunity,
                                listing_summary=top_listing_summary,
                                item_image_url=cheapest_icon_url,
                            )
                            alert_state[item_key] = (cycle, low_mirror)
                            if variant_id:
                                storage.upsert_price_alert_cooldown(
                                    variant_id=variant_id,
                                    last_cycle=cycle,
                                    last_low_mirror=float(low_mirror),
                                )

            # Append median (not lowest) to history — more stable baseline.
            if median_mirror is not None:
                history.append(median_mirror)
                price_history[item_key] = history[-(alert_config.history_cycles * 3):]

        index_width = len(str(total_items))
        index_label = f"{index:>{index_width}}/{total_items}"
        mode_label = item_mode_label(item)
        mode_field = f"[{mode_label}]"
        name_field = item.name
        total_field = f"total={total_results:>3}"
        log_line(
            "item",
            f"[{index_label}] {name_field} {mode_field} {total_field}  cheapest={cheapest_text}",
        )
        if xfer or instant or online_ni or relist or nib or repr_seller or multi_fp or new_rows:
            log_line(
                "inference",
                f"           inference: xfer={xfer} instant_sale={instant} non_inst_online={online_ni} "
                f"relist={relist} non_instant_offline?={nib} reprice={repr_seller} multi_seller_fp={multi_fp} new_rows={new_rows}",
            )

    return divines_per_mirror


def seconds_until_next_tick(start_monotonic: float, delay_seconds: int, now_monotonic: float) -> float:
    elapsed = max(0.0, now_monotonic - start_monotonic)
    slot = math.floor(elapsed / delay_seconds) + 1
    next_tick = start_monotonic + (slot * delay_seconds)
    return max(0.0, next_tick - now_monotonic)


def _install_poller_log_tee() -> None:
    import sys

    server_dir = Path(__file__).resolve().parent.parent / "server"
    sd = str(server_dir)
    if sd not in sys.path:
        sys.path.insert(0, sd)
    from structured_logging import install_structured_logging

    install_structured_logging("poller", "poller.log")


def main() -> None:
    _install_poller_log_tee()
    cfg = parse_args()
    items_file = Path(DEFAULT_ITEMS_FILE)

    session = build_session()
    rate_limiter = AdaptiveRateLimiter()

    root_dir = Path(__file__).resolve().parents[1]
    storage = StorageService(root_dir=root_dir)
    variants = storage.list_variants()
    if not variants:
        # Bootstrap from items.txt once.
        item_specs = load_item_specs(items_file)
        variant_specs: list[VariantSpec] = []
        for idx, spec in enumerate(item_specs):
            mode = item_mode_token(spec)
            display = f"{spec.name} Normal" if mode == "normal" else spec.name
            variant_specs.append(
                VariantSpec(
                    base_item_name=spec.name,
                    mode=mode,
                    display_name=display,
                    sort_order=idx,
                    icon_path=None,
                )
            )
        storage.upsert_variants(variant_specs)
        variants = storage.list_variants()

    variant_ids_by_key = {f"{name}::{mode}": int(vid) for (vid, name, mode, _dn, _so, _ic) in variants}
    item_specs = []
    for (_vid, name, mode, _dn, _so, _ic) in variants:
        alt = True if mode == "aa" else False if mode == "normal" else None
        item_specs.append(ItemSpec(name=name, alternate_art=alt))

    log_line("cycle", f"Loaded {len(item_specs)} item(s) from SQLite (bootstrap file: {items_file}).")
    log_line("cycle", f"Polling every {cfg.poll_interval} seconds aligned to start-time grid. Press Ctrl+C to stop.")

    start_monotonic = time.monotonic()
    # Persist cycle numbers across restarts so DB time series accumulates points.
    cycle = storage.latest_cycle_number(league=DEFAULT_LEAGUE)
    cycles_done = 0
    divines_per_mirror = DIVINES_PER_MIRROR

    # Load alert config and seed price history from SQLite.
    alert_config = load_alert_config()
    price_history = seed_price_history_from_db(storage, alert_config.history_cycles)
    alert_state = seed_price_alert_cooldown_from_db(storage, variant_ids_by_key)
    log_line("cycle", f"Loaded {len(alert_state)} persisted price-alert cooldown entries.")
    logged_sales_webhook_cfg = False
    logged_db_export_webhook_cfg = False
    log_line(
        "cycle",
        "Alert config: "
        f"enabled={alert_config.enabled}, "
        f"threshold={alert_config.threshold_pct}%, "
        f"history_cycles={alert_config.history_cycles}, "
        f"min_total_results={alert_config.min_total_results}, "
        f"min_floor_listings={alert_config.min_floor_listings}, "
        f"floor_band_pct={alert_config.floor_band_pct}%, "
        f"low_liquidity_extra_drop_pct={alert_config.low_liquidity_extra_drop_pct}%, "
        f"cooldown_cycles={alert_config.cooldown_cycles}",
    )
    if alert_config.enabled and not alert_config.webhook_url:
        log_line(
            "warn",
            "Alerts are enabled but no Discord webhook secret is set. "
            "Set DISCORD_WEBHOOK_URL (or POE_DISCORD_WEBHOOK_URL).",
        )

    while cfg.max_cycles is None or cycles_done < cfg.max_cycles:
        cycle += 1
        cycles_done += 1
        # Console display: use local time for readability (not stored, console-only)
        cycle_start = datetime.now().strftime("%H:%M:%S")
        print("")
        log_line("cycle", f"Cycle {cycle} start at {cycle_start}")
        run_started_at_utc = datetime.now(timezone.utc).isoformat()

        # Reload alert config each cycle so UI changes take effect without restart.
        alert_config = load_alert_config()
        sales_webhook_url, sales_webhook_dedicated = load_discord_sales_webhook_url_from_env()
        sales_window_days = load_sales_discord_window_days(storage)
        db_export_webhook_url = load_discord_db_export_webhook_url_from_env()
        if sales_webhook_url and not logged_sales_webhook_cfg:
            msg = f"Sales Discord webhook active; est.-sold window={sales_window_days}d"
            if not sales_webhook_dedicated:
                msg += (
                    "; using main alert webhook (set DISCORD_WEBHOOK_URL_SALES for a separate channel)"
                )
            log_line("cycle", msg)
            logged_sales_webhook_cfg = True
        if db_export_webhook_url and not logged_db_export_webhook_cfg:
            log_line("cycle", "DB export webhook active; will upload a DB snapshot once every 24h.")
            logged_db_export_webhook_cfg = True

        try:
            divines_per_mirror = run_cycle(
                session, rate_limiter, item_specs, cycle,
                storage, variant_ids_by_key, run_started_at_utc,
                divines_per_mirror, price_history, alert_state, alert_config,
                inference_fetch_cap=cfg.inference_fetch_cap,
                sales_webhook_url=sales_webhook_url,
                sales_window_days=sales_window_days,
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            log_line("error", f"HTTP error during cycle {cycle}: status={status} error={exc}")
        except requests.RequestException as exc:
            log_line("error", f"Request error during cycle {cycle}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log_line("error", f"Unexpected error during cycle {cycle}: {exc}")

        if db_export_webhook_url:
            maybe_export_db_to_discord(
                storage=storage,
                session=session,
                cfg=DbExportConfig(
                    webhook_url=db_export_webhook_url,
                    tz_offset_minutes=120,  # GMT+2
                    schedule_hour=12,
                    schedule_minute=0,
                ),
                log=log_line,
            )

        if cfg.max_cycles is not None and cycles_done >= cfg.max_cycles:
            log_line("cycle", f"Reached max-cycles ({cfg.max_cycles}). Stopping.")
            break

        sleep_seconds = seconds_until_next_tick(
            start_monotonic=start_monotonic,
            delay_seconds=cfg.poll_interval,
            now_monotonic=time.monotonic(),
        )
        # Console display: use local time for readability (not stored, console-only)
        next_run = datetime.now().timestamp() + sleep_seconds
        next_run_text = datetime.fromtimestamp(next_run).strftime("%H:%M:%S")
        log_line("cycle", f"Cycle {cycle} complete. Sleeping {int(sleep_seconds)}s until {next_run_text}")
        time.sleep(sleep_seconds)


