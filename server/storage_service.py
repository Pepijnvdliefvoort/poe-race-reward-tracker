from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage.db import Database
from storage.repos import ConfigRepo, ItemsRepo, PollsRepo, VisitorsRepo


ROOT_DIR = Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_positive_mirror(v: Any) -> bool:
    if v is None:
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def _last_known_mirror_fields_from_history(points: list[dict[str, Any]]) -> dict[str, float]:
    """Newest-first scan of polls before the last row; per field, first valid mirror value wins."""
    out: dict[str, float] = {}
    if len(points) < 2:
        return out
    for key in ("lowestMirror", "medianMirror", "highestMirror"):
        for p in reversed(points[:-1]):
            v = p.get(key)
            if _valid_positive_mirror(v):
                out[key] = float(v)
                break
    return out


def _attach_last_known_mirror_prices(item: dict[str, Any]) -> None:
    """
    When the latest poll has no usable mirror prices (e.g. 0 listings), expose the most recent
    prior values on ``latest`` so the UI can show last known instead of n/a.
    """
    points = item.get("points") or []
    latest = item.get("latest")
    if not isinstance(latest, dict) or len(points) < 1:
        return
    lk = _last_known_mirror_fields_from_history(points)
    lk_field = {
        "lowestMirror": "lastKnownLowestMirror",
        "medianMirror": "lastKnownMedianMirror",
        "highestMirror": "lastKnownHighestMirror",
    }
    for cur, lk_key in lk_field.items():
        if not _valid_positive_mirror(latest.get(cur)) and cur in lk:
            latest[lk_key] = lk[cur]


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
                    "sales": [],
                }

            sales_by_variant_id: dict[int, list[dict[str, Any]]] = {}
            if variant_ids_by_key:
                vids: list[int] = []
                for _k, vid in variant_ids_by_key.items():
                    if isinstance(vid, int) and vid > 0:
                        vids.append(vid)
                if vids:
                    placeholders = ",".join("?" * len(vids))
                    sale_rows = con.execute(
                        f"""
                        SELECT
                          s.item_variant_id,
                          s.occurred_at_utc,
                          s.mirror_equiv,
                          s.price_amount,
                          s.price_currency,
                          pr.divines_per_mirror
                        FROM sales s
                        JOIN item_polls ip ON ip.id = s.item_poll_id
                        JOIN poll_runs pr ON pr.id = ip.poll_run_id
                        WHERE s.item_variant_id IN ({placeholders})
                          AND reverted_at_utc IS NULL
                          AND mirror_equiv IS NOT NULL
                        ORDER BY s.item_variant_id ASC, s.occurred_at_utc ASC
                        """,
                        vids,
                    ).fetchall()
                    for sr in sale_rows:
                        try:
                            vid = int(sr["item_variant_id"])
                        except (TypeError, ValueError):
                            continue
                        t_ms = epoch_ms(str(sr["occurred_at_utc"] or ""))
                        if t_ms is None:
                            continue
                        try:
                            m = float(sr["mirror_equiv"] or 0.0)
                        except (TypeError, ValueError):
                            m = 0.0
                        amt = sr["price_amount"]
                        try:
                            amount = float(amt) if amt is not None else None
                        except (TypeError, ValueError):
                            amount = None
                        cur_raw = sr["price_currency"]
                        currency = str(cur_raw).strip().lower() if isinstance(cur_raw, str) and cur_raw.strip() else None
                        dpm_raw = sr["divines_per_mirror"]
                        try:
                            divines_per_mirror = float(dpm_raw) if dpm_raw is not None else None
                        except (TypeError, ValueError):
                            divines_per_mirror = None
                        sales_by_variant_id.setdefault(vid, []).append(
                            # Keep decimal mirror prices (e.g. 0.37 mirrors) so points land correctly.
                            {
                                "time": t_ms,
                                # `price` is the chart Y value (kept compact for stable axes/ticks).
                                "price": round(m, 2),
                                # Keep an unrounded-ish mirror-equiv for accurate tooltip conversions.
                                "mirrorEquiv": round(m, 6),
                                "priceAmount": round(amount, 4) if amount is not None else None,
                                "priceCurrency": currency,
                                "divinesPerMirror": round(divines_per_mirror, 4)
                                if divines_per_mirror is not None
                                else None,
                            }
                        )

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
                        "inferenceLikelyNonInstantOnline": int(r["inf_likely_non_instant_online"] or 0),
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

            for variant_key, variant_id in variant_ids_by_key.items():
                if variant_key not in items:
                    continue
                it = items[variant_key]
                try:
                    vid = int(variant_id)
                except (TypeError, ValueError):
                    it["sales"] = []
                    continue
                it["sales"] = list(sales_by_variant_id.get(vid, []))

            item_list = list(items.values())
            for it in item_list:
                _attach_last_known_mirror_prices(it)
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

    def fetch_listing_preview(self, query_id: str, *, limit: int | None = 20) -> dict[str, Any]:
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
            snap_rows = polls.listing_snapshot_rows(item_poll_id=item_poll_id, limit=limit)
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

    def fetch_account_compare(
        self,
        *,
        accounts: list[str],
        mode: str = "all",
        currency: str = "mirror",
        instant_only: bool = True,
    ) -> dict[str, Any]:
        """
        Compare market-lowest listings vs lowest listings for given seller accounts.

        Data source is `listing_snapshots` for the latest poll of each item variant.
        This is a public, unauthenticated endpoint intended for quick self-checks.
        """
        cleaned_accounts: list[str] = []
        seen = set()
        for a in accounts or []:
            s = str(a or "").strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            cleaned_accounts.append(s)
        # Keep query size reasonable.
        cleaned_accounts = cleaned_accounts[:6]

        mode_norm = str(mode or "all").strip().lower()
        if mode_norm not in {"all", "diff"}:
            mode_norm = "all"

        # UI displays values in mirror equivalents, matching the main dashboard logic.
        cur_norm = "mirror"

        con = self.connect()
        con.row_factory = sqlite3.Row
        try:
            # Base rows: every variant, plus latest poll metadata (queryId + updatedAt).
            variants = con.execute(
                """
                WITH latest_poll AS (
                  SELECT item_variant_id, MAX(id) AS item_poll_id
                  FROM item_polls
                  GROUP BY item_variant_id
                )
                SELECT
                  v.id AS variant_id,
                  i.name AS item_name,
                  v.display_name,
                  v.mode,
                  v.sort_order,
                  ip.query_id,
                  ip.requested_at_utc AS updated_at_utc
                FROM item_variants v
                JOIN items i ON i.id = v.item_id
                LEFT JOIN latest_poll lp ON lp.item_variant_id = v.id
                LEFT JOIN item_polls ip ON ip.id = lp.item_poll_id
                ORDER BY v.sort_order ASC, v.display_name ASC
                """
            ).fetchall()

            # Market mins for latest poll per variant (mirror equivalents across currencies).
            market_rows = con.execute(
                """
                WITH latest_poll AS (
                  SELECT item_variant_id, MAX(id) AS item_poll_id
                  FROM item_polls
                  GROUP BY item_variant_id
                )
                SELECT
                  ip.item_variant_id AS variant_id,
                  MIN(
                    CASE
                      WHEN LOWER(TRIM(ls.currency)) IN ('mirror', 'mirrors', 'mirror of kalandra') THEN ls.amount
                      WHEN LOWER(TRIM(ls.currency)) IN ('divine', 'divines', 'div', 'divine orb', 'divine orbs')
                        THEN ls.amount / NULLIF(pr.divines_per_mirror, 0)
                      WHEN LOWER(TRIM(ls.currency)) IN ('exalted', 'exalt', 'exa', 'exalted orb', 'exalted orbs')
                        THEN (ls.amount / 60.0) / NULLIF(pr.divines_per_mirror, 0)
                      ELSE NULL
                    END
                  ) AS market_min
                FROM latest_poll lp
                JOIN item_polls ip ON ip.id = lp.item_poll_id
                JOIN poll_runs pr ON pr.id = ip.poll_run_id
                JOIN listing_snapshots ls ON ls.item_poll_id = ip.id
                WHERE ls.amount IS NOT NULL
                  AND ls.amount > 0
                  AND (? = 0 OR ls.is_instant_buyout = 1)
                GROUP BY ip.item_variant_id
                """,
                (1 if instant_only else 0,),
            ).fetchall()
            market_min_by_variant = {int(r["variant_id"]): (float(r["market_min"]) if r["market_min"] is not None else None) for r in market_rows}

            account_min_by_variant: dict[int, dict[str, float]] = {}
            if cleaned_accounts:
                placeholders = ",".join("?" for _ in cleaned_accounts)
                sql = f"""
                WITH latest_poll AS (
                  SELECT item_variant_id, MAX(id) AS item_poll_id
                  FROM item_polls
                  GROUP BY item_variant_id
                )
                SELECT
                  ip.item_variant_id AS variant_id,
                  ls.seller_name,
                  MIN(
                    CASE
                      WHEN LOWER(TRIM(ls.currency)) IN ('mirror', 'mirrors', 'mirror of kalandra') THEN ls.amount
                      WHEN LOWER(TRIM(ls.currency)) IN ('divine', 'divines', 'div', 'divine orb', 'divine orbs')
                        THEN ls.amount / NULLIF(pr.divines_per_mirror, 0)
                      WHEN LOWER(TRIM(ls.currency)) IN ('exalted', 'exalt', 'exa', 'exalted orb', 'exalted orbs')
                        THEN (ls.amount / 60.0) / NULLIF(pr.divines_per_mirror, 0)
                      ELSE NULL
                    END
                  ) AS account_min
                FROM latest_poll lp
                JOIN item_polls ip ON ip.id = lp.item_poll_id
                JOIN poll_runs pr ON pr.id = ip.poll_run_id
                JOIN listing_snapshots ls ON ls.item_poll_id = ip.id
                WHERE ls.amount IS NOT NULL
                  AND ls.amount > 0
                  AND (? = 0 OR ls.is_instant_buyout = 1)
                  AND ls.seller_name IN ({placeholders})
                GROUP BY ip.item_variant_id, ls.seller_name
                """
                params: list[Any] = [1 if instant_only else 0]
                params.extend(cleaned_accounts)
                acct_rows = con.execute(sql, params).fetchall()
                for r in acct_rows:
                    vid = int(r["variant_id"])
                    seller = str(r["seller_name"] or "").strip()
                    amt = r["account_min"]
                    if not seller or amt is None:
                        continue
                    account_min_by_variant.setdefault(vid, {})[seller] = float(amt)

            def _differs(a: float | None, b: float | None) -> bool:
                if a is None or b is None:
                    return False
                return abs(float(a) - float(b)) > 1e-9

            rows: list[dict[str, Any]] = []
            for v in variants:
                vid = int(v["variant_id"])
                market_min = market_min_by_variant.get(vid)
                acct_map = account_min_by_variant.get(vid, {})

                if mode_norm == "diff":
                    # Include only if at least one account has a price and it differs from market.
                    keep = False
                    for acct in cleaned_accounts:
                        keep = keep or _differs(acct_map.get(acct), market_min)
                    if not keep:
                        continue

                row = {
                    "variantId": vid,
                    "itemName": str(v["item_name"] or ""),
                    "displayName": str(v["display_name"] or ""),
                    "mode": str(v["mode"] or ""),
                    "sortOrder": int(v["sort_order"] or 0),
                    "queryId": str(v["query_id"] or ""),
                    "updatedAt": str(v["updated_at_utc"] or ""),
                    "market": {"currency": cur_norm, "amount": market_min},
                    "accounts": {acct: acct_map.get(acct) for acct in cleaned_accounts},
                }
                rows.append(row)

            return {
                "ok": True,
                "generatedAt": _utc_now_iso(),
                "mode": mode_norm,
                "currency": cur_norm,
                "instantOnly": bool(instant_only),
                "accounts": cleaned_accounts,
                "rows": rows,
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

