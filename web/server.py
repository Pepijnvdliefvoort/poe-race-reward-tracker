from __future__ import annotations

import csv
import json
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "web"
CSV_PATH = ROOT_DIR / "price_poll.csv"
ITEMS_FILE = ROOT_DIR / "items.txt"
HOST = "127.0.0.1"
PORT = 8080
POLL_INTERVAL_SECONDS = 3600


def _parse_float(value: str) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_epoch_ms(timestamp_utc: str) -> int | None:
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
        return f"{item_name} AA"
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
                "sortOrder": variant.get("order", float("inf")),
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
    """Generate local image filename for item. Strips apostrophes and spaces."""
    clean_name = item_name.replace("'", "").replace(" ", "")
    
    if is_aa is None or is_aa is True:
        # Try AA version first
        aa_path = WEB_DIR / "assets" / "icons" / f"{clean_name}Alt.png"
        if aa_path.exists():
            return f"/assets/icons/{clean_name}Alt.png"
    
    if is_aa is None or is_aa is False:
        # Try normal version
        normal_path = WEB_DIR / "assets" / "icons" / f"{clean_name}.png"
        if normal_path.exists():
            return f"/assets/icons/{clean_name}.png"
    
    return None


def _calculate_next_poll_time() -> int | None:
    """Calculate next poll time from latest observed cycle start + poll interval.

    Cycle start is anchored to the first configured item in items.txt. This keeps
    next-poll tracking correct even if cycle numbers restart after poller restarts.
    """
    if not CSV_PATH.exists():
        return None

    try:
        variants_by_name, _ = _load_item_variants()
        first_variant: dict[str, Any] | None = None
        for variants in variants_by_name.values():
            for variant in variants:
                if first_variant is None or variant.get("order", float("inf")) < first_variant.get("order", float("inf")):
                    first_variant = variant

        first_variant_key = str(first_variant.get("key")) if first_variant else None
        latest_first_item_time: datetime | None = None
        latest_cycle: int | None = None
        earliest_in_latest_cycle: datetime | None = None

        with CSV_PATH.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                cycle_raw = (row.get("cycle") or "").strip()
                timestamp_utc = (row.get("timestamp_utc") or "").strip()
                if not cycle_raw or not timestamp_utc:
                    continue

                try:
                    cycle = int(cycle_raw)
                    dt = datetime.fromisoformat(timestamp_utc)
                except ValueError:
                    continue

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                row_item_name = (row.get("item_name") or "").strip()
                row_mode = _parse_row_mode(row.get("item_mode"))
                row_key = f"{row_item_name}::{_mode_token(row_mode)}"
                if first_variant_key and row_key == first_variant_key:
                    latest_first_item_time = dt

                if latest_cycle is None or cycle > latest_cycle:
                    latest_cycle = cycle
                    earliest_in_latest_cycle = dt
                elif cycle == latest_cycle and earliest_in_latest_cycle is not None and dt < earliest_in_latest_cycle:
                    earliest_in_latest_cycle = dt

        cycle_start_time = latest_first_item_time or earliest_in_latest_cycle

        if cycle_start_time is None:
            return None

        next_dt = cycle_start_time + timedelta(seconds=POLL_INTERVAL_SECONDS)
        return int(next_dt.timestamp() * 1000)
    except Exception:
        return None


def load_price_data() -> dict[str, Any]:
    variants_by_name, order_by_key = _load_item_variants()
    items: dict[str, dict[str, Any]] = _seed_items_from_variants(variants_by_name)

    if not CSV_PATH.exists():
        item_list = list(items.values())
        item_list.sort(
            key=lambda it: (
                it.get("sortOrder", float("inf")),
                it.get("itemName") or "",
            )
        )

        return {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "csvPath": str(CSV_PATH),
            "rowCount": 0,
            "items": item_list,
            "nextPollTime": _calculate_next_poll_time(),
            "warning": "CSV file not found.",
        }

    row_count = 0
    # Persist variant mapping by query id so AA/normal does not drift when cycle numbers
    # reset after restarting the poller.
    query_variant_map: dict[str, dict[str, dict[str, Any]]] = {}
    name_variant_counts: dict[str, int] = {}

    with CSV_PATH.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_count += 1
            item_name = (row.get("item_name") or "").strip()
            if not item_name:
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
                        "order": float("inf"),
                    }

            if variant is None:
                variant = {
                    "itemName": item_name,
                    "displayName": _display_name(item_name, row_mode),
                    "isAA": row_mode,
                    "key": f"{item_name}::{row_mode_token}",
                    "order": order_by_key.get(f"{item_name}::{row_mode_token}", float("inf")),
                }

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
                str(variant["key"]),
                {
                    "itemName": str(variant["displayName"]),
                    "baseItemName": item_name,
                    "mode": _mode_token(variant.get("isAA")),
                    "imagePath": _get_image_path(item_name, variant.get("isAA")),
                    "sortOrder": variant.get("order", float("inf")),
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
            }
            series["points"].append(point)
            series["latest"] = point

    item_list = list(items.values())

    for item in item_list:
        item["points"].sort(key=lambda p: p["time"])

    item_list.sort(
        key=lambda it: (
            it.get("sortOrder", float("inf")),
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


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/prices":
            payload = load_price_data()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path in {"/", ""}:
            self.path = "/index.html"

        return super().do_GET()


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Serving dashboard at http://{HOST}:{PORT}")
    print(f"Reading live data from: {CSV_PATH}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
