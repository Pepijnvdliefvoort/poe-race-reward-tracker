"""Detect likely PoE account bans from multi-listing vanishes + trade API confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import requests

from poller.sale_inference_engine import VanishedListing
from server.sales_discord_notify import send_account_banned_notification
from storage.repos import BanCandidateVanishRow
from storage.service import StorageService


@dataclass(frozen=True)
class AccountBanConfig:
    enabled: bool = True
    min_vanishes: int = 2
    cycle_lookback: int = 1


@dataclass(frozen=True)
class BanWatchUser:
    seller_name: str
    user_id: str


def ban_candidate_vanish_events(
    *,
    item_key: str,
    cycle: int,
    vanishes: list[VanishedListing],
) -> list[dict[str, Any]]:
    return [
        {
            "rule": "ban_candidate_vanish",
            "itemKey": item_key,
            "fingerprint": v.fingerprint,
            "seller": v.seller,
            "mirrorEquiv": v.mirror_equiv,
            "isInstant": v.is_instant,
            "cycle": int(cycle),
        }
        for v in vanishes
    ]


def _group_vanishes_by_seller(
    rows: list[BanCandidateVanishRow],
) -> dict[str, list[BanCandidateVanishRow]]:
    grouped: dict[str, list[BanCandidateVanishRow]] = {}
    seen: dict[str, set[tuple[int, str]]] = {}
    for row in rows:
        seller = str(row.seller).strip()
        if not seller:
            continue
        key = (int(row.item_variant_id), str(row.fingerprint))
        seller_seen = seen.setdefault(seller, set())
        if key in seller_seen:
            continue
        seller_seen.add(key)
        grouped.setdefault(seller, []).append(row)
    return grouped


def load_account_ban_config(storage: StorageService) -> AccountBanConfig:
    try:
        data = storage.get_config(key="market") or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def _bool(key: str, default: bool) -> bool:
        if key not in data:
            return default
        raw = data.get(key)
        if isinstance(raw, bool):
            return raw
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(float(data.get(key, default)))
        except Exception:
            v = default
        return max(lo, min(hi, v))

    return AccountBanConfig(
        enabled=_bool("account_ban_check_enabled", True),
        min_vanishes=_int("account_ban_min_vanishes", 2, 2, 50),
        cycle_lookback=_int("account_ban_cycle_lookback", 1, 1, 10),
    )


def process_cycle_ban_checks(
    *,
    session: requests.Session,
    storage: StorageService,
    cycle: int,
    league: str,
    bans_webhook_url: str,
    probe_account_has_zero_listings: Callable[..., bool | None],
    config: AccountBanConfig | None = None,
    watch_mentions_content: str | None = None,
    watch_mention_ids: list[str] | None = None,
    watch_users: list[BanWatchUser] | None = None,
    log_line: Callable[[str, str], None] | None = None,
) -> None:
    cfg = config or AccountBanConfig()
    if not cfg.enabled:
        return

    min_cycle = max(1, int(cycle) - int(cfg.cycle_lookback))
    rows = storage.list_ban_candidate_vanishes_for_cycles(
        league=str(league),
        min_cycle=min_cycle,
        max_cycle=int(cycle),
    )
    grouped = _group_vanishes_by_seller(rows)

    for seller, seller_rows in grouped.items():
        if len(seller_rows) < int(cfg.min_vanishes):
            continue

        probe_result = probe_account_has_zero_listings(account_name=seller)
        if probe_result is None:
            if log_line:
                log_line("warn", f"Account ban probe failed for {seller}; skipping ban handling")
            continue
        if not probe_result:
            if storage.is_account_ban_alerted(seller=seller):
                storage.clear_account_ban_alerted(seller=seller)
            continue

        if storage.is_account_ban_alerted(seller=seller):
            continue

        revert_poll_id = max(int(r.item_poll_id) for r in seller_rows)
        reverted = storage.revert_sales_for_account_ban(
            seller=seller,
            vanish_rows=seller_rows,
            revert_poll_id=revert_poll_id,
        )

        if log_line:
            log_line(
                "alert",
                (
                    f"Account likely banned: {seller} "
                    f"({len(seller_rows)} vanished listing(s) in cycles {min_cycle}-{cycle}, "
                    f"trade total=0); reverted {len(reverted)} sale(s)"
                ),
            )

        mention_content = watch_mentions_content
        mention_ids = list(watch_mention_ids or [])
        if watch_users:
            for w in watch_users:
                if str(w.seller_name).casefold() == seller.casefold():
                    uid = str(w.user_id).strip()
                    if uid and uid not in mention_ids:
                        mention_ids.append(uid)
            if mention_ids and not mention_content:
                mention_content = " ".join(f"<@{uid}>" for uid in mention_ids)

        if bans_webhook_url:
            try:
                send_account_banned_notification(
                    session,
                    webhook_url=bans_webhook_url,
                    account_name=seller,
                    vanished_items=seller_rows,
                    content=mention_content,
                    allowed_user_ids=mention_ids or None,
                )
            except Exception as exc:  # noqa: BLE001
                if log_line:
                    log_line("warn", f"Failed ban Discord webhook for {seller}: {exc}")

        storage.mark_account_ban_alerted(seller=seller, alert_cycle=int(cycle))
