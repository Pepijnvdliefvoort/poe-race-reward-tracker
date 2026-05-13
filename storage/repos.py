from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ItemVariantRow:
    id: int
    item_name: str
    mode: str  # aa|normal|any
    display_name: str
    sort_order: int | None
    icon_path: str | None


class ItemsRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def upsert_item(self, *, name: str, icon_path: str | None, created_at_utc: str) -> int:
        row = self._con.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()
        if row:
            item_id = int(row["id"])
            self._con.execute("UPDATE items SET icon_path = COALESCE(?, icon_path) WHERE id = ?", (icon_path, item_id))
            return item_id

        cur = self._con.execute(
            "INSERT INTO items(name, icon_path, created_at_utc) VALUES (?, ?, ?)",
            (name, icon_path, created_at_utc),
        )
        return int(cur.lastrowid)

    def upsert_variant(
        self,
        *,
        item_id: int,
        mode: str,
        display_name: str,
        sort_order: int | None,
        active_from_utc: str | None = None,
        active_to_utc: str | None = None,
    ) -> int:
        row = self._con.execute(
            "SELECT id FROM item_variants WHERE item_id = ? AND mode = ?",
            (item_id, mode),
        ).fetchone()
        if row:
            variant_id = int(row["id"])
            self._con.execute(
                "UPDATE item_variants SET display_name = ?, sort_order = ?, active_from_utc = ?, active_to_utc = ? WHERE id = ?",
                (display_name, sort_order, active_from_utc, active_to_utc, variant_id),
            )
            return variant_id

        cur = self._con.execute(
            "INSERT INTO item_variants(item_id, mode, display_name, sort_order, active_from_utc, active_to_utc) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, mode, display_name, sort_order, active_from_utc, active_to_utc),
        )
        return int(cur.lastrowid)

    def list_variants(self) -> list[ItemVariantRow]:
        rows = self._con.execute(
            """
            SELECT v.id, i.name AS item_name, v.mode, v.display_name, v.sort_order, i.icon_path
            FROM item_variants v
            JOIN items i ON i.id = v.item_id
            ORDER BY v.sort_order ASC, v.display_name ASC
            """
        ).fetchall()
        return [
            ItemVariantRow(
                id=int(r["id"]),
                item_name=str(r["item_name"]),
                mode=str(r["mode"]),
                display_name=str(r["display_name"]),
                sort_order=(int(r["sort_order"]) if r["sort_order"] is not None else None),
                icon_path=(str(r["icon_path"]) if r["icon_path"] is not None else None),
            )
            for r in rows
        ]

    def set_icon_path_for_variant(self, *, variant_id: int, icon_path: str) -> None:
        self._con.execute(
            """
            UPDATE items
               SET icon_path = ?
             WHERE id = (
                 SELECT item_id
                   FROM item_variants
                  WHERE id = ?
             )
            """,
            (icon_path, int(variant_id)),
        )


class ConfigRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def get_json(self, *, key: str) -> dict[str, Any] | None:
        row = self._con.execute("SELECT value_json FROM app_config WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            parsed = json.loads(str(row["value_json"] or ""))
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def set_json(self, *, key: str, value: dict[str, Any], updated_at_utc: str) -> None:
        self._con.execute(
            """
            INSERT INTO app_config(key, value_json, updated_at_utc)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value_json=excluded.value_json,
              updated_at_utc=excluded.updated_at_utc
            """,
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True), updated_at_utc),
        )


class VisitorsRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def insert_visit(self, *, ts_utc: str, ip: str, path: str) -> None:
        self._con.execute(
            "INSERT INTO visits(ts_utc, ip, path) VALUES (?, ?, ?)",
            (ts_utc, ip, path),
        )

    def aggregate_visits(self) -> tuple[dict[str, int], dict[str, str]]:
        # Returns (counts_by_ip, last_seen_by_ip)
        rows = self._con.execute(
            """
            SELECT ip, COUNT(1) AS visits, MAX(ts_utc) AS last_seen
            FROM visits
            GROUP BY ip
            """
        ).fetchall()
        counts: dict[str, int] = {}
        last: dict[str, str] = {}
        for r in rows:
            ip = str(r["ip"] or "").strip()
            if not ip:
                continue
            counts[ip] = int(r["visits"] or 0)
            if r["last_seen"]:
                last[ip] = str(r["last_seen"])
        return counts, last

    def get_geo(self, *, ip: str) -> dict[str, float] | None:
        row = self._con.execute("SELECT lat, lon FROM ip_geo_cache WHERE ip = ?", (ip,)).fetchone()
        if not row:
            return None
        try:
            return {"lat": float(row["lat"]), "lon": float(row["lon"])}
        except Exception:
            return None

    def set_geo(self, *, ip: str, lat: float, lon: float, updated_at_utc: str) -> None:
        self._con.execute(
            """
            INSERT INTO ip_geo_cache(ip, lat, lon, updated_at_utc)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
              lat=excluded.lat,
              lon=excluded.lon,
              updated_at_utc=excluded.updated_at_utc
            """,
            (ip, float(lat), float(lon), updated_at_utc),
        )


class PollsRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def upsert_poll_run(
        self,
        *,
        cycle_number: int,
        league: str,
        started_at_utc: str,
        divines_per_mirror: float | None,
        top_ids_limit: int | None,
        inference_fetch_cap: int | None,
        app_version: str | None = None,
    ) -> int:
        row = self._con.execute(
            "SELECT id FROM poll_runs WHERE cycle_number = ? AND league = ?",
            (cycle_number, league),
        ).fetchone()
        if row:
            run_id = int(row["id"])
            self._con.execute(
                """
                UPDATE poll_runs
                SET started_at_utc = ?, divines_per_mirror = ?, top_ids_limit = ?, inference_fetch_cap = ?, app_version = ?
                WHERE id = ?
                """,
                (started_at_utc, divines_per_mirror, top_ids_limit, inference_fetch_cap, app_version, run_id),
            )
            return run_id

        cur = self._con.execute(
            """
            INSERT INTO poll_runs(cycle_number, league, started_at_utc, divines_per_mirror, top_ids_limit, inference_fetch_cap, app_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cycle_number, league, started_at_utc, divines_per_mirror, top_ids_limit, inference_fetch_cap, app_version),
        )
        return int(cur.lastrowid)

    def insert_item_poll(
        self,
        *,
        poll_run_id: int,
        item_variant_id: int,
        requested_at_utc: str,
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
        inf_confirmed_transfer: int,
        inf_likely_instant_sale: int,
        inf_likely_non_instant_online: int,
        inf_relist_same_seller: int,
        inf_non_instant_removed: int,
        inf_reprice_same_seller: int,
        inf_multi_seller_same_fingerprint: int,
        inf_new_listing_rows: int,
        fetched_for_inference: int,
    ) -> int:
        # Replace-on-conflict so reruns of same cycle stay idempotent.
        cur = self._con.execute(
            """
            INSERT INTO item_polls(
              poll_run_id, item_variant_id, requested_at_utc, query_id, total_results, used_results, unsupported_price_count,
              mirror_count, lowest_mirror, median_mirror, highest_mirror,
              divine_count, lowest_divine, median_divine, highest_divine,
              inf_confirmed_transfer, inf_likely_instant_sale, inf_likely_non_instant_online, inf_relist_same_seller, inf_non_instant_removed,
              inf_reprice_same_seller, inf_multi_seller_same_fingerprint, inf_new_listing_rows,
              fetched_for_inference
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(poll_run_id, item_variant_id) DO UPDATE SET
              requested_at_utc=excluded.requested_at_utc,
              query_id=excluded.query_id,
              total_results=excluded.total_results,
              used_results=excluded.used_results,
              unsupported_price_count=excluded.unsupported_price_count,
              mirror_count=excluded.mirror_count,
              lowest_mirror=excluded.lowest_mirror,
              median_mirror=excluded.median_mirror,
              highest_mirror=excluded.highest_mirror,
              divine_count=excluded.divine_count,
              lowest_divine=excluded.lowest_divine,
              median_divine=excluded.median_divine,
              highest_divine=excluded.highest_divine,
              inf_confirmed_transfer=excluded.inf_confirmed_transfer,
              inf_likely_instant_sale=excluded.inf_likely_instant_sale,
              inf_likely_non_instant_online=excluded.inf_likely_non_instant_online,
              inf_relist_same_seller=excluded.inf_relist_same_seller,
              inf_non_instant_removed=excluded.inf_non_instant_removed,
              inf_reprice_same_seller=excluded.inf_reprice_same_seller,
              inf_multi_seller_same_fingerprint=excluded.inf_multi_seller_same_fingerprint,
              inf_new_listing_rows=excluded.inf_new_listing_rows,
              fetched_for_inference=excluded.fetched_for_inference
            """,
            (
                poll_run_id,
                item_variant_id,
                requested_at_utc,
                query_id,
                int(total_results),
                int(used_results),
                int(unsupported_price_count),
                int(mirror_count),
                lowest_mirror,
                median_mirror,
                highest_mirror,
                int(divine_count),
                lowest_divine,
                median_divine,
                highest_divine,
                int(inf_confirmed_transfer),
                int(inf_likely_instant_sale),
                int(inf_likely_non_instant_online),
                int(inf_relist_same_seller),
                int(inf_non_instant_removed),
                int(inf_reprice_same_seller),
                int(inf_multi_seller_same_fingerprint),
                int(inf_new_listing_rows),
                int(fetched_for_inference),
            ),
        )
        # On SQLite, `lastrowid` is not reliable when the insert takes the DO UPDATE path.
        last = int(cur.lastrowid or 0)
        if last > 0:
            return last
        row = self._con.execute(
            "SELECT id FROM item_polls WHERE poll_run_id = ? AND item_variant_id = ?",
            (poll_run_id, item_variant_id),
        ).fetchone()
        if not row:
            raise RuntimeError("Upserted item_polls row but could not resolve id.")
        return int(row["id"])

    def replace_listing_snapshots(self, *, item_poll_id: int, rows: list[dict[str, Any]]) -> None:
        self._con.execute("DELETE FROM listing_snapshots WHERE item_poll_id = ?", (item_poll_id,))
        payload: list[tuple] = []
        for rank, r in enumerate(rows):
            payload.append(
                (
                    item_poll_id,
                    rank,
                    str(r.get("sellerName") or "unknown"),
                    str(r.get("priceText") or ""),
                    r.get("amount"),
                    str(r.get("currency") or "unknown"),
                    int(r.get("listingCount") or 1),
                    1 if bool(r.get("isInstantBuyout")) else 0,
                    1 if bool(r.get("isCorrupted")) else 0,
                    r.get("posted"),
                    r.get("indexed"),
                    r.get("fingerprint"),
                )
            )
        self._con.executemany(
            """
            INSERT INTO listing_snapshots(
                            item_poll_id, rank, seller_name, price_text, amount, currency, listing_count, is_instant_buyout, is_corrupted, posted, indexed, fingerprint
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            payload,
        )

    def replace_inference_events(self, *, item_poll_id: int, events: list[dict[str, Any]]) -> None:
        self._con.execute("DELETE FROM inference_events WHERE item_poll_id = ?", (item_poll_id,))
        payload: list[tuple] = []
        for e in events:
            rule = str(e.get("rule") or "")
            payload.append(
                (
                    item_poll_id,
                    rule,
                    e.get("fingerprint"),
                    e.get("seller"),
                    e.get("from_seller"),
                    e.get("to_seller"),
                    e.get("prevMirrorEquiv"),
                    e.get("currMirrorEquiv"),
                    e.get("count"),
                    json.dumps(e, ensure_ascii=False),
                )
            )
        self._con.executemany(
            """
            INSERT INTO inference_events(
              item_poll_id, rule, fingerprint, seller, from_seller, to_seller, prev_mirror_equiv, curr_mirror_equiv, count, meta_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            payload,
        )

    def latest_item_poll_by_query_id(self, *, query_id: str) -> sqlite3.Row | None:
        return self._con.execute(
            """
            SELECT ip.*, pr.league
            FROM item_polls ip
            JOIN poll_runs pr ON pr.id = ip.poll_run_id
            WHERE ip.query_id = ?
            ORDER BY ip.requested_at_utc DESC
            LIMIT 1
            """,
            (query_id,),
        ).fetchone()

    def listing_snapshot_rows(self, *, item_poll_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        if limit is None:
            return self._con.execute(
                "SELECT * FROM listing_snapshots WHERE item_poll_id = ? ORDER BY rank ASC",
                (item_poll_id,),
            ).fetchall()

        lim = int(limit)
        if lim <= 0:
            return []

        return self._con.execute(
            "SELECT * FROM listing_snapshots WHERE item_poll_id = ? ORDER BY rank ASC LIMIT ?",
            (item_poll_id, lim),
        ).fetchall()

    def clear_market_tables(self) -> None:
        # Order matters due to FKs.
        # Also clear persisted inference working set (otherwise future cycles will be biased
        # by signals/pending entries from the previous dataset).
        self._con.execute("DELETE FROM inference_state_pending")
        self._con.execute("DELETE FROM inference_state_signals")
        self._con.execute("DELETE FROM inference_events")
        self._con.execute("DELETE FROM listing_snapshots")
        self._con.execute("DELETE FROM item_polls")
        self._con.execute("DELETE FROM poll_runs")


class InferenceStateRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def load_state(
        self, *, item_variant_id: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        sig_rows = self._con.execute(
            """
            SELECT fingerprint, seller, is_instant, mirror_equiv, price_amount, price_currency, seller_online
            FROM inference_state_signals
            WHERE item_variant_id = ?
            """,
            (item_variant_id,),
        ).fetchall()
        pend_rows = self._con.execute(
            """
            SELECT fingerprint, seller, removed_cycle, counted_immediate, mirror_equiv, price_amount, price_currency, pending_kind
            FROM inference_state_pending
            WHERE item_variant_id = ?
            """,
            (item_variant_id,),
        ).fetchall()
        signals = [
            {
                "fingerprint": str(r["fingerprint"]),
                "seller": str(r["seller"]),
                "isInstant": bool(int(r["is_instant"] or 0)),
                "sellerOnline": bool(int(r["seller_online"] or 0)),
                "mirrorEquiv": (float(r["mirror_equiv"]) if r["mirror_equiv"] is not None else None),
                "priceAmount": (float(r["price_amount"]) if r["price_amount"] is not None else None),
                "priceCurrency": (str(r["price_currency"]) if r["price_currency"] is not None else None),
            }
            for r in sig_rows
        ]
        pending_instant: list[dict[str, Any]] = []
        pending_online: list[dict[str, Any]] = []
        for r in pend_rows:
            kind = str(r["pending_kind"] or "instant")
            row = {
                "fingerprint": str(r["fingerprint"]),
                "seller": str(r["seller"]),
                "removed_cycle": int(r["removed_cycle"] or 0),
                "countedImmediate": bool(int(r["counted_immediate"] or 0)),
                "mirrorEquiv": (float(r["mirror_equiv"]) if r["mirror_equiv"] is not None else None),
                "priceAmount": (float(r["price_amount"]) if r["price_amount"] is not None else None),
                "priceCurrency": (str(r["price_currency"]) if r["price_currency"] is not None else None),
            }
            if kind == "online_non_instant":
                pending_online.append(row)
            else:
                pending_instant.append(row)
        return signals, pending_instant, pending_online

    def save_state(
        self,
        *,
        item_variant_id: int,
        cycle: int,
        curr_signals: list[dict[str, Any]],
        pending_instant: list[dict[str, Any]],
        pending_online: list[dict[str, Any]],
    ) -> None:
        self._con.execute("DELETE FROM inference_state_signals WHERE item_variant_id = ?", (item_variant_id,))
        self._con.execute("DELETE FROM inference_state_pending WHERE item_variant_id = ?", (item_variant_id,))

        sig_payload: list[tuple] = []
        seen_sig: set[tuple[str, str]] = set()
        for s in curr_signals:
            fp = str(s.get("fingerprint") or "")
            seller = str(s.get("seller") or "")
            if not fp or not seller:
                continue
            key = (fp, seller)
            if key in seen_sig:
                # Same seller can list multiple identical items; state tracks the (fingerprint,seller) pair.
                continue
            seen_sig.add(key)
            sig_payload.append(
                (
                    item_variant_id,
                    fp,
                    seller,
                    1 if bool(s.get("isInstant")) else 0,
                    s.get("mirrorEquiv"),
                    s.get("priceAmount"),
                    (str(s.get("priceCurrency") or "").strip().lower() or None),
                    cycle,
                    1 if bool(s.get("sellerOnline")) else 0,
                )
            )
        self._con.executemany(
            """
            INSERT INTO inference_state_signals(
              item_variant_id, fingerprint, seller, is_instant, mirror_equiv, price_amount, price_currency, last_seen_cycle, seller_online
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            sig_payload,
        )

        pend_payload: list[tuple] = []
        seen_pend: set[tuple[str, str, str]] = set()

        def _append_pending(p: dict[str, Any], kind: str) -> None:
            fp = str(p.get("fingerprint") or "")
            seller = str(p.get("seller") or "")
            if not fp or not seller:
                return
            key = (fp, seller, kind)
            if key in seen_pend:
                return
            seen_pend.add(key)
            pend_payload.append(
                (
                    item_variant_id,
                    fp,
                    seller,
                    int(p.get("removed_cycle") or 0),
                    1 if bool(p.get("countedImmediate")) else 0,
                    p.get("mirrorEquiv"),
                    p.get("priceAmount"),
                    (str(p.get("priceCurrency") or "").strip().lower() or None),
                    kind,
                )
            )

        for p in pending_instant:
            _append_pending(p, "instant")
        for p in pending_online:
            _append_pending(p, "online_non_instant")

        self._con.executemany(
            """
            INSERT INTO inference_state_pending(
              item_variant_id, fingerprint, seller, removed_cycle, counted_immediate, mirror_equiv, price_amount, price_currency, pending_kind
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            pend_payload,
        )


class SalesRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def insert_sale(
        self,
        *,
        item_poll_id: int,
        item_variant_id: int,
        occurred_at_utc: str,
        recorded_at_utc: str,
        rule: str,
        fingerprint: str | None,
        seller: str,
        buyer: str | None,
        price_amount: float | None,
        price_currency: str | None,
        mirror_equiv: float | None,
        quantity: int = 1,
    ) -> None:
        fp = (fingerprint or "").strip()
        b = (buyer or "").strip()
        cur = (price_currency or "").strip().lower() or None
        qty = int(quantity) if int(quantity) > 0 else 1
        self._con.execute(
            """
            INSERT OR IGNORE INTO sales(
              item_poll_id, item_variant_id, occurred_at_utc, recorded_at_utc,
              rule, fingerprint, seller, buyer,
              price_amount, price_currency, mirror_equiv, quantity
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(item_poll_id),
                int(item_variant_id),
                str(occurred_at_utc),
                str(recorded_at_utc),
                str(rule),
                fp,
                str(seller),
                b,
                price_amount,
                cur,
                mirror_equiv,
                qty,
            ),
        )

    def revert_latest_sale(
        self,
        *,
        item_variant_id: int,
        rule: str,
        fingerprint: str,
        seller: str,
        min_occurred_at_utc: str | None = None,
        occurred_at_or_before_utc: str,
        reverted_at_utc: str,
        reverted_by_item_poll_id: int,
        reverted_reason: str,
    ) -> str | None:
        """
        Mark the latest matching inferred-sale row (instant or non-instant 4b) as reverted on relist.

        Returns the reverted sale's ``occurred_at_utc`` timestamp, or None when no row matched.
        """
        r = str(rule)
        if r not in {"likely_instant_sale", "likely_non_instant_online_sale"}:
            return None
        min_cutoff = str(min_occurred_at_utc) if isinstance(min_occurred_at_utc, str) and min_occurred_at_utc.strip() else None
        row = self._con.execute(
            """
            SELECT s.id, s.occurred_at_utc
            FROM sales s
            WHERE s.item_variant_id = ?
              AND s.rule = ?
              AND s.fingerprint = ?
              AND s.seller = ?
              AND s.buyer = ''
              AND s.reverted_at_utc IS NULL
              AND (? IS NULL OR s.occurred_at_utc >= ?)
              AND s.occurred_at_utc <= ?
            ORDER BY s.occurred_at_utc DESC, s.id DESC
            LIMIT 1
            """,
            (
                int(item_variant_id),
                r,
                str(fingerprint),
                str(seller),
                min_cutoff,
                min_cutoff,
                str(occurred_at_or_before_utc),
            ),
        ).fetchone()
        if not row:
            return None

        cur = self._con.execute(
            """
            UPDATE sales
            SET reverted_at_utc = ?,
                reverted_by_item_poll_id = ?,
                reverted_reason = ?
            WHERE id = ?
              AND reverted_at_utc IS NULL
            """,
            (
                str(reverted_at_utc),
                int(reverted_by_item_poll_id),
                str(reverted_reason),
                int(row["id"]),
            ),
        )
        if int(cur.rowcount or 0) <= 0:
            return None
        return str(row["occurred_at_utc"])


    def confirmed_transfer_exists_after(
        self,
        *,
        item_variant_id: int,
        fingerprint: str,
        from_seller: str,
        after_utc: str,
        before_or_at_utc: str,
    ) -> bool:
        """
        Return True if a confirmed_transfer event for (fingerprint, from_seller) was recorded
        after ``after_utc`` and at or before ``before_or_at_utc``.

        Used to guard late relist reversions: if the item was observed under a different seller
        after the inferred sale, the seller's new listing is a new copy, not a relist.
        """
        row = self._con.execute(
            """
            SELECT 1
            FROM inference_events ie
            JOIN item_polls ip ON ip.id = ie.item_poll_id
            WHERE ip.item_variant_id = ?
              AND ie.rule = 'confirmed_transfer'
              AND ie.fingerprint = ?
              AND ie.from_seller = ?
              AND ip.requested_at_utc > ?
              AND ip.requested_at_utc <= ?
            LIMIT 1
            """,
            (
                int(item_variant_id),
                str(fingerprint),
                str(from_seller),
                str(after_utc),
                str(before_or_at_utc),
            ),
        ).fetchone()
        return row is not None


class PriceAlertCooldownRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def load_all(self) -> list[tuple[int, int, float]]:
        rows = self._con.execute(
            """
            SELECT item_variant_id, last_alert_cycle, last_alert_low_mirror
            FROM price_alert_cooldown
            """
        ).fetchall()
        out: list[tuple[int, int, float]] = []
        for r in rows:
            out.append(
                (
                    int(r["item_variant_id"]),
                    int(r["last_alert_cycle"]),
                    float(r["last_alert_low_mirror"]),
                )
            )
        return out

    def upsert(
        self,
        *,
        item_variant_id: int,
        last_alert_cycle: int,
        last_alert_low_mirror: float,
        updated_at_utc: str,
    ) -> None:
        self._con.execute(
            """
            INSERT INTO price_alert_cooldown(
                item_variant_id, last_alert_cycle, last_alert_low_mirror, updated_at_utc
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(item_variant_id) DO UPDATE SET
                last_alert_cycle = excluded.last_alert_cycle,
                last_alert_low_mirror = excluded.last_alert_low_mirror,
                updated_at_utc = excluded.updated_at_utc
            """,
            (int(item_variant_id), int(last_alert_cycle), float(last_alert_low_mirror), str(updated_at_utc)),
        )

