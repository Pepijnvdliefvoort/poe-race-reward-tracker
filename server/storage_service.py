from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage.db import Database
from storage.repos import ConfigRepo, ItemsRepo, PollsRepo, VisitorsRepo


ROOT_DIR = Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServerStorage:
    def __init__(self, root_dir: Path | None = None) -> None:
        self._db = Database(root_dir=root_dir or ROOT_DIR)

    @property
    def db_path(self) -> Path:
        return self._db.path

    def connect(self) -> sqlite3.Connection:
        return self._db.connect()

    def has_any_variants(self) -> bool:
        con = self.connect()
        try:
            row = con.execute("SELECT 1 FROM item_variants LIMIT 1").fetchone()
            return bool(row)
        finally:
            con.close()

    def variant_ids_by_key(self) -> dict[str, int]:
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT v.id AS variant_id, i.name AS item_name, v.mode
                FROM item_variants v
                JOIN items i ON i.id = v.item_id
                """
            ).fetchall()
            out: dict[str, int] = {}
            for r in rows:
                key = f"{str(r['item_name'])}::{str(r['mode'])}"
                out[key] = int(r["variant_id"])
            return out
        finally:
            con.close()

    def variants_for_ui_fallback(self, *, items_fallback: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """
        Return UI variant dicts. If DB has variants, use them; else return provided fallback.
        """
        con = self.connect()
        try:
            rows = ItemsRepo(con).list_variants()
            if not rows:
                return items_fallback
            out: dict[str, dict[str, Any]] = {}
            for r in rows:
                key = f"{r.item_name}::{r.mode}"
                out[key] = {
                    "itemName": r.item_name,
                    "displayName": r.display_name,
                    "isAA": True if r.mode == "aa" else False if r.mode == "normal" else None,
                    "key": key,
                    "order": r.sort_order,
                    "mode": r.mode,
                }
            return out
        finally:
            con.close()

    def get_market_config(self) -> dict[str, Any] | None:
        con = self.connect()
        try:
            return ConfigRepo(con).get_json(key="market")
        finally:
            con.close()

    def set_market_config(self, cfg: dict[str, Any]) -> None:
        con = self.connect()
        try:
            ConfigRepo(con).set_json(key="market", value=cfg, updated_at_utc=_utc_now_iso())
            con.commit()
        finally:
            con.close()

    def latest_poll_run_started_at(self) -> str | None:
        con = self.connect()
        try:
            row = con.execute("SELECT started_at_utc FROM poll_runs ORDER BY started_at_utc DESC LIMIT 1").fetchone()
            return str(row["started_at_utc"]) if row and row["started_at_utc"] else None
        finally:
            con.close()

    def record_visit(self, *, ts_utc: str, ip: str, path: str) -> None:
        con = self.connect()
        try:
            VisitorsRepo(con).insert_visit(ts_utc=ts_utc, ip=ip, path=path)
            con.commit()
        finally:
            con.close()

    def visitor_aggregate(self) -> tuple[dict[str, int], dict[str, str]]:
        con = self.connect()
        try:
            return VisitorsRepo(con).aggregate_visits()
        finally:
            con.close()

    def geo_get(self, *, ip: str) -> dict[str, float] | None:
        con = self.connect()
        try:
            return VisitorsRepo(con).get_geo(ip=ip)
        finally:
            con.close()

    def geo_set(self, *, ip: str, lat: float, lon: float, updated_at_utc: str) -> None:
        con = self.connect()
        try:
            VisitorsRepo(con).set_geo(ip=ip, lat=lat, lon=lon, updated_at_utc=updated_at_utc)
            con.commit()
        finally:
            con.close()

    def load_price_payload_points(
        self,
        *,
        variant_ids_by_key: dict[str, int],
        variants_by_key: dict[str, dict[str, Any]],
        order_by_key: dict[str, int],
        get_image_path: Any,
        epoch_ms: Any,
    ) -> dict[str, Any]:
        """
        Build the same payload shape as `data_service.load_price_data()`, but from SQLite.
        """
        con = self.connect()
        try:
            # points per variant id
            items: dict[str, dict[str, Any]] = {}
            allowed_keys = set(variants_by_key.keys())

            for variant_key, variant in variants_by_key.items():
                if variant_key not in allowed_keys:
                    continue
                items[variant_key] = {
                    "itemName": str(variant.get("displayName") or variant.get("itemName") or ""),
                    "baseItemName": str(variant.get("itemName") or ""),
                    "mode": str(variant.get("mode") or ""),
                    "imagePath": get_image_path(str(variant.get("itemName") or ""), variant.get("isAA")),
                    "sortOrder": variant.get("order"),
                    "points": [],
                    "latest": None,
                    "queryId": None,
                }

            for variant_key, variant_id in variant_ids_by_key.items():
                if variant_key not in items:
                    continue
                rows = con.execute(
                    """
                    SELECT ip.*, pr.cycle_number AS cycle
                    FROM item_polls ip
                    JOIN poll_runs pr ON pr.id = ip.poll_run_id
                    WHERE ip.item_variant_id = ?
                    ORDER BY ip.requested_at_utc ASC
                    """,
                    (variant_id,),
                ).fetchall()

                series = items[variant_key]
                for r in rows:
                    t_ms = epoch_ms(str(r["requested_at_utc"] or ""))
                    if t_ms is None:
                        continue
                    point = {
                        "time": t_ms,
                        "cycle": int(r["cycle"] or 0),
                        "lowestMirror": r["lowest_mirror"],
                        "medianMirror": r["median_mirror"],
                        "highestMirror": r["highest_mirror"],
                        "lowestDivine": r["lowest_divine"],
                        "medianDivine": r["median_divine"],
                        "highestDivine": r["highest_divine"],
                        "totalResults": int(r["total_results"] or 0),
                        "usedResults": int(r["used_results"] or 0),
                        "inferenceConfirmedTransfer": int(r["inf_confirmed_transfer"] or 0),
                        "inferenceLikelyInstantSale": int(r["inf_likely_instant_sale"] or 0),
                        "inferenceRelistSameSeller": int(r["inf_relist_same_seller"] or 0),
                        "inferenceNonInstantRemoved": int(r["inf_non_instant_removed"] or 0),
                        "inferenceRepriceSameSeller": int(r["inf_reprice_same_seller"] or 0),
                        "inferenceMultiSellerSameFingerprint": int(r["inf_multi_seller_same_fingerprint"] or 0),
                        "inferenceNewListingRows": int(r["inf_new_listing_rows"] or 0),
                    }
                    series["points"].append(point)
                    # Always advance `latest` to the most recent poll point, even when no price
                    # was available (e.g. 0 results). The UI uses `latest.time`/`latest.cycle`
                    # to compute the poll cycle highlight ("next in line"). Price availability
                    # is handled separately by `getAvailableLowestPrice()`.
                    series["latest"] = point
                    qid = str(r["query_id"] or "").strip()
                    if qid:
                        series["queryId"] = qid

            item_list = list(items.values())
            item_list.sort(
                key=lambda it: (
                    order_by_key.get(f"{it.get('baseItemName','')}::{it.get('mode','')}", 10**9),
                    it.get("itemName") or "",
                )
            )

            return {
                "generatedAt": _utc_now_iso(),
                "dbPath": str(self.db_path),
                "rowCount": sum(len(it.get("points") or []) for it in item_list),
                "items": item_list,
            }
        finally:
            con.close()

    def fetch_listing_preview(self, query_id: str) -> dict[str, Any]:
        cleaned = (query_id or "").strip()
        if not cleaned:
            raise ValueError("queryId is required")
        con = self.connect()
        try:
            polls = PollsRepo(con)
            row = polls.latest_item_poll_by_query_id(query_id=cleaned)
            if not row:
                return {"queryId": cleaned, "league": "Standard", "totalResults": 0, "listings": [], "source": "db-not-found"}
            item_poll_id = int(row["id"])
            snap_rows = polls.listing_snapshot_rows(item_poll_id=item_poll_id)
            listings = []
            for r in snap_rows:
                listings.append(
                    {
                        "priceText": str(r["price_text"] or ""),
                        "amount": r["amount"],
                        "currency": str(r["currency"] or "unknown"),
                        "isInstantBuyout": bool(int(r["is_instant_buyout"] or 0)),
                        "sellerName": str(r["seller_name"] or "unknown"),
                        "posted": r["posted"],
                        "indexed": r["indexed"],
                        "fingerprint": r["fingerprint"],
                    }
                )
            return {
                "queryId": cleaned,
                "league": str(row["league"] or "Standard"),
                "totalResults": int(row["total_results"] or 0),
                "listings": listings,
                "updatedAt": str(row["requested_at_utc"] or ""),
                "source": "db",
            }
        finally:
            con.close()

    def clear_market_data(self) -> dict[str, Any]:
        con = self.connect()
        try:
            PollsRepo(con).clear_market_tables()
            con.commit()
            return {"cleared": {"sqlite": True}}
        finally:
            con.close()

