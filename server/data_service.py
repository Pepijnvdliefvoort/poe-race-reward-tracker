from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from server.storage_service import ServerStorage

ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "web"
ITEMS_FILE = ROOT_DIR / "items.txt"  # bootstrap only (DB is source of truth)
CONFIG_PATH = ROOT_DIR / "config.json"  # bootstrap only (DB is source of truth)
POLL_INTERVAL_SECONDS = 3600

DEFAULT_CONFIG = {
    "alert_enabled": False,
    "alert_require_flip_signal": True,
    "alert_threshold_pct": 30,
    "alert_history_cycles": 10,
    "alert_min_total_results": 10,
    "alert_min_floor_listings": 2,
    "alert_floor_band_pct": 7.5,
    "alert_low_liquidity_extra_drop_pct": 20.0,
    "alert_cooldown_cycles": 6,
    "sales_discord_window_days": 90,
}


def load_config() -> dict:
    storage = ServerStorage(ROOT_DIR)
    # DB-first
    try:
        cfg = storage.get_market_config()
        if cfg is not None:
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
            return merged
    except Exception:
        pass
    # Bootstrap from legacy file once
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
            try:
                storage.set_market_config(merged)
            except Exception:
                pass
            return merged
        except Exception:
            return dict(DEFAULT_CONFIG)
    # Fresh clone: persist defaults so admin UI can load/edit immediately.
    merged = dict(DEFAULT_CONFIG)
    try:
        storage.set_market_config(merged)
    except Exception:
        pass
    return merged


def save_config(data: dict) -> None:
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    ServerStorage(ROOT_DIR).set_market_config(merged)


def _parse_nonneg_int(value: str | None) -> int:
    if value is None:
        return 0
    v = str(value).strip()
    if not v:
        return 0
    try:
        n = int(float(v))
    except ValueError:
        return 0
    return max(0, n)


def _parse_signed_int(value: str | None) -> int:
    """Like int() parse for CSV fields; negatives allowed (e.g. inference relist undo)."""
    if value is None:
        return 0
    v = str(value).strip()
    if not v:
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def _parse_float(value: str) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    except ValueError:
        return None


def _order_or_max(order: Any) -> int:
    if isinstance(order, int):
        return order
    if isinstance(order, float) and math.isfinite(order):
        return int(order)
    return 10**9


def _to_epoch_ms(timestamp_utc: str) -> int | None:
    """
    Convert UTC ISO 8601 timestamp string to epoch milliseconds.
    Input: UTC timestamp string from CSV (e.g., "2026-04-16T12:00:00+00:00")
    Output: Unix epoch milliseconds (UTC-based, safe for JavaScript Date constructor)
    Frontend will convert this to local time automatically via toLocaleTimeString().
    """
    if not timestamp_utc:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_utc)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _parse_mode(raw_mode: str | None) -> bool | None:
    if raw_mode is None:
        return True

    mode = raw_mode.strip().lower()
    if mode in {"", "aa", "alt", "alternate", "alternate-art", "alternate_art", "true"}:
        return True
    if mode in {"normal", "non-alt", "non_alt", "regular", "false"}:
        return False
    if mode in {"any", "either", "all"}:
        return None
    return True


def _mode_token(is_aa: bool | None) -> str:
    if is_aa is True:
        return "aa"
    if is_aa is False:
        return "normal"
    return "any"


def _display_name(item_name: str, is_aa: bool | None) -> str:
    if is_aa is True:
        return f"{item_name}"
    if is_aa is False:
        return f"{item_name} Normal"
    return item_name


def _parse_row_mode(raw_mode: str | None) -> bool | None:
    if raw_mode is None:
        return None

    mode = raw_mode.strip().lower()
    if not mode:
        return None
    if mode in {"aa", "alt", "alternate", "alternate-art", "alternate_art", "true"}:
        return True
    if mode in {"normal", "non-alt", "non_alt", "regular", "false"}:
        return False
    if mode in {"any", "either", "all"}:
        return None
    return None


def _mode_to_is_aa(mode: str | None) -> bool | None:
    if mode == "aa":
        return True
    if mode == "normal":
        return False
    return None


def _seed_items_from_variants(variants_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for variants in variants_by_name.values():
        for variant in variants:
            mode = _mode_token(variant.get("isAA"))
            base_item_name = str(variant.get("itemName") or "")
            variant_key = str(variant.get("key") or f"{base_item_name}::{mode}")
            items[variant_key] = {
                "itemName": str(variant.get("displayName") or base_item_name),
                "baseItemName": base_item_name,
                "mode": mode,
                "imagePath": _get_image_path(base_item_name, _mode_to_is_aa(mode)),
                "sortOrder": variant.get("order"),
                "points": [],
                "latest": None,
                "queryId": None,
            }
    return items


def _load_item_variants() -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """Load variant order from items.txt preserving duplicates (AA/normal)."""
    variants_by_name: dict[str, list[dict[str, Any]]] = {}
    order_by_key: dict[str, int] = {}

    if not ITEMS_FILE.exists():
        return variants_by_name, order_by_key

    try:
        index = 0
        with ITEMS_FILE.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue

                parts = [part.strip() for part in line.split("|", 1)]
                item_name = parts[0]
                if not item_name:
                    continue

                raw_mode = parts[1] if len(parts) == 2 else None
                is_aa = _parse_mode(raw_mode)
                mode = _mode_token(is_aa)
                variant_key = f"{item_name}::{mode}"
                variant = {
                    "itemName": item_name,
                    "displayName": _display_name(item_name, is_aa),
                    "isAA": is_aa,
                    "key": variant_key,
                    "order": index,
                }

                variants_by_name.setdefault(item_name, []).append(variant)
                order_by_key[variant_key] = index
                index += 1
    except Exception:
        pass

    return variants_by_name, order_by_key


def _get_image_path(item_name: str, is_aa: bool | None) -> str | None:
    """Generate local image filename for item with Linux-safe case-insensitive matching."""
    if is_aa is None or is_aa is True:
        aa_name = _resolve_icon_filename(item_name, is_alt=True)
        if aa_name:
            return f"/assets/icons/{aa_name}"

    # If alt art is missing, gracefully fall back to normal icon when available.
    if is_aa is None or is_aa is False or is_aa is True:
        normal_name = _resolve_icon_filename(item_name, is_alt=False)
        if normal_name:
            return f"/assets/icons/{normal_name}"

    return None


def _normalize_icon_token(value: str) -> str:
    return value.replace("'", "").replace(" ", "").replace("-", "").lower()


@lru_cache(maxsize=1)
def _icon_index() -> dict[str, str]:
    icons_dir = WEB_DIR / "assets" / "icons"
    mapping: dict[str, str] = {}

    if not icons_dir.exists():
        return mapping

    for file_path in icons_dir.iterdir():
        if not file_path.is_file() or file_path.suffix.lower() != ".png":
            continue
        mapping[_normalize_icon_token(file_path.stem)] = file_path.name

    return mapping


def _resolve_icon_filename(item_name: str, is_alt: bool) -> str | None:
    suffix = "Alt" if is_alt else ""
    wanted = _normalize_icon_token(f"{item_name}{suffix}")
    return _icon_index().get(wanted)


def _calculate_next_poll_time() -> int | None:
    """Calculate next poll time from latest poll_run started_at + poll interval."""
    try:
        last_started = ServerStorage(ROOT_DIR).latest_poll_run_started_at()
        if not last_started:
            return None
        dt = datetime.fromisoformat(last_started)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        next_dt = dt + timedelta(seconds=POLL_INTERVAL_SECONDS)
        return int(next_dt.timestamp() * 1000)
    except Exception:
        return None


def fetch_listing_preview(query_id: str, *, limit: int | None = 20) -> dict[str, Any]:
    return ServerStorage(ROOT_DIR).fetch_listing_preview(query_id, limit=limit)


def fetch_account_compare(*, accounts: list[str], mode: str = "all", top_n: int = 5) -> dict[str, Any]:
    return ServerStorage(ROOT_DIR).fetch_account_compare(accounts=accounts, mode=mode, top_n=top_n)


def load_price_data() -> dict[str, Any]:
    """
    Load price data from CSV and return as JSON payload.
    All timestamps in returned data are in epoch milliseconds (UTC-based).
    Frontend receives milliseconds and displays in browser's local timezone.
    """
    # DB is the source of truth for tracked items. If empty, bootstrap-import from items.txt once.
    variants_by_name, order_by_key = _load_item_variants()
    items: dict[str, dict[str, Any]] = _seed_items_from_variants(variants_by_name)

    storage = ServerStorage(ROOT_DIR)
    from storage.service import StorageService, VariantSpec  # local import

    ss = StorageService(root_dir=ROOT_DIR)
    if not storage.has_any_variants():
        # Bootstrap from items.txt definitions (the same parsing you already had)
        variant_specs = []
        for base_name, variants in variants_by_name.items():
            for v in variants:
                mode = _mode_token(v.get("isAA"))
                variant_specs.append(
                    VariantSpec(
                        base_item_name=base_name,
                        mode=mode,
                        display_name=str(v.get("displayName") or base_name),
                        sort_order=_order_or_max(v.get("order")),
                        icon_path=_get_image_path(base_name, v.get("isAA")),
                    )
                )
        ss.upsert_variants(variant_specs)

    variant_ids_by_key = storage.variant_ids_by_key()
    items_by_key = storage.variants_for_ui_fallback(items_fallback=items)
    payload = storage.load_price_payload_points(
        variant_ids_by_key=variant_ids_by_key,
        variants_by_key=items_by_key,
        order_by_key=order_by_key,
        get_image_path=_get_image_path,
        epoch_ms=_to_epoch_ms,
    )
    payload["nextPollTime"] = _calculate_next_poll_time()
    return payload

    row_count = 0
    # Persist variant mapping by query id so AA/normal does not drift when cycle numbers reset.
    query_variant_map: dict[str, dict[str, dict[str, Any]]] = {}
    name_variant_counts: dict[str, int] = {}

    with CSV_PATH.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_count += 1
            item_name = (row.get("item_name") or "").strip()
            if not item_name:
                continue
            # Only include items currently declared in items.txt.
            if item_name not in variants_by_name:
                continue

            name_variants = variants_by_name.get(item_name, [])
            row_mode = _parse_row_mode(row.get("item_mode"))
            row_mode_token = _mode_token(row_mode)
            query_id = (row.get("query_id") or "").strip()
            mapped_variants = query_variant_map.setdefault(item_name, {})

            variant: dict[str, Any] | None = None
            if name_variants and row.get("item_mode") is not None:
                wanted_key = f"{item_name}::{row_mode_token}"
                for candidate in name_variants:
                    if str(candidate.get("key")) == wanted_key:
                        variant = candidate
                        break

            if variant is None and query_id:
                variant = mapped_variants.get(query_id)

            if variant is None:
                seen_count = name_variant_counts.get(item_name, 0)
                name_variant_counts[item_name] = seen_count + 1

                if seen_count < len(name_variants):
                    variant = name_variants[seen_count]
                elif name_variants:
                    variant = name_variants[-1]
                else:
                    variant_mode = None
                    variant = {
                        "itemName": item_name,
                        "displayName": item_name,
                        "isAA": variant_mode,
                        "key": f"{item_name}::{_mode_token(variant_mode)}",
                        "order": None,
                    }

            if variant is None:
                variant = {
                    "itemName": item_name,
                    "displayName": _display_name(item_name, row_mode),
                    "isAA": row_mode,
                    "key": f"{item_name}::{row_mode_token}",
                    "order": order_by_key.get(f"{item_name}::{row_mode_token}"),
                }

            variant_key = str(variant.get("key") or "")
            if variant_key not in allowed_variant_keys:
                continue

            if query_id:
                mapped_variants[query_id] = variant

            try:
                cycle = int(row.get("cycle") or 0)
            except ValueError:
                cycle = 0

            t_ms = _to_epoch_ms((row.get("timestamp_utc") or "").strip())
            if t_ms is None:
                continue

            series = items.setdefault(
                variant_key,
                {
                    "itemName": str(variant["displayName"]),
                    "baseItemName": item_name,
                    "mode": _mode_token(variant.get("isAA")),
                    "imagePath": _get_image_path(item_name, variant.get("isAA")),
                    "sortOrder": variant.get("order"),
                    "points": [],
                    "latest": None,
                    "queryId": None,
                },
            )

            if query_id:
                series["queryId"] = query_id

            point = {
                "time": t_ms,
                "cycle": cycle,
                "lowestMirror": _parse_float(row.get("lowest_mirror") or ""),
                "medianMirror": _parse_float(row.get("median_mirror") or ""),
                "highestMirror": _parse_float(row.get("highest_mirror") or ""),
                "lowestDivine": _parse_float(row.get("lowest_divine") or ""),
                "medianDivine": _parse_float(row.get("median_divine") or ""),
                "highestDivine": _parse_float(row.get("highest_divine") or ""),
                "totalResults": int(row.get("total_results") or 0),
                "usedResults": int(row.get("used_results") or 0),
                "inferenceConfirmedTransfer": _parse_nonneg_int(row.get("inference_confirmed_transfer")),
                "inferenceLikelyInstantSale": _parse_signed_int(row.get("inference_likely_instant_sale")),
                "inferenceLikelyNonInstantOnline": _parse_signed_int(row.get("inference_likely_non_instant_online")),
                "inferenceRelistSameSeller": _parse_nonneg_int(row.get("inference_relist_same_seller")),
                "inferenceNonInstantRemoved": _parse_nonneg_int(row.get("inference_non_instant_removed")),
                "inferenceRepriceSameSeller": _parse_nonneg_int(row.get("inference_reprice_same_seller")),
                "inferenceMultiSellerSameFingerprint": _parse_nonneg_int(
                    row.get("inference_multi_seller_same_fingerprint")
                ),
                "inferenceNewListingRows": _parse_nonneg_int(row.get("inference_new_listing_rows")),
            }
            series["points"].append(point)
            # Always advance `latest` to the most recent poll point so the UI can
            # correctly infer poll order even when price fields are missing.
            series["latest"] = point

    item_list = list(items.values())

    for item in item_list:
        item["points"].sort(key=lambda p: p["time"])

    item_list.sort(
        key=lambda it: (
            _order_or_max(it.get("sortOrder")),
            it.get("itemName") or "",
        )
    )

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "csvPath": str(CSV_PATH),
        "rowCount": row_count,
        "items": item_list,
        "nextPollTime": _calculate_next_poll_time(),
    }
