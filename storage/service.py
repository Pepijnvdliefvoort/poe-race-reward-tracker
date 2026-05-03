from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .repos import ConfigRepo, InferenceStateRepo, ItemsRepo, PollsRepo, PriceAlertCooldownRepo, SalesRepo


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VariantSpec:
    base_item_name: str
    mode: str  # aa|normal|any
    display_name: str
    sort_order: int | None
    icon_path: str | None

    @property
    def key(self) -> str:
        return f"{self.base_item_name}::{self.mode}"


@dataclass(frozen=True)
class PollWriteResult:
    item_poll_id: int
    inference_counts: dict[str, int]
    inference_events: list[dict[str, Any]]


class StorageService:
    def __init__(self, *, root_dir: Path) -> None:
        self._db = Database(root_dir=root_dir)

    @property
    def db_path(self) -> Path:
        return self._db.path

    def ensure_initialized(self) -> None:
        self._db.ensure_initialized()

    def upsert_variants(self, variants: list[VariantSpec]) -> dict[str, int]:
        """
        Ensure `items` + `item_variants` exist, returning mapping: variant_key -> variant_id.
        """
        self.ensure_initialized()
        mapping: dict[str, int] = {}
        con = self._db.connect()
        try:
            items = ItemsRepo(con)
            now = _utc_now_iso()
            for v in variants:
                item_id = items.upsert_item(name=v.base_item_name, icon_path=v.icon_path, created_at_utc=now)
                variant_id = items.upsert_variant(
                    item_id=item_id,
                    mode=v.mode,
                    display_name=v.display_name,
                    sort_order=v.sort_order,
                )
                mapping[v.key] = variant_id
            con.commit()
            return mapping
        finally:
            con.close()

    def load_inference_state(
        self, *, variant_id: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Returns (signals, pending_instant, pending_online_non_instant)."""
        self.ensure_initialized()
        con = self._db.connect()
        try:
            repo = InferenceStateRepo(con)
            return repo.load_state(item_variant_id=variant_id)
        finally:
            con.close()

    def get_config(self, *, key: str) -> dict[str, Any] | None:
        self.ensure_initialized()
        con = self._db.connect()
        try:
            return ConfigRepo(con).get_json(key=key)
        finally:
            con.close()

    def set_config(self, *, key: str, value: dict[str, Any]) -> None:
        self.ensure_initialized()
        con = self._db.connect()
        try:
            ConfigRepo(con).set_json(key=key, value=value, updated_at_utc=_utc_now_iso())
            con.commit()
        finally:
            con.close()

    def list_variants(self) -> list[tuple[int, str, str, str, int | None, str | None]]:
        """
        Return [(variant_id, base_item_name, mode, display_name, sort_order, icon_path), ...]
        """
        self.ensure_initialized()
        con = self._db.connect()
        try:
            rows = ItemsRepo(con).list_variants()
            return [(r.id, r.item_name, r.mode, r.display_name, r.sort_order, r.icon_path) for r in rows]
        finally:
            con.close()

    def update_variant_icon_path(self, *, variant_id: int, icon_path: str) -> None:
        self.ensure_initialized()
        path = str(icon_path or "").strip()
        if int(variant_id) <= 0 or not path:
            return
        con = self._db.connect()
        try:
            ItemsRepo(con).set_icon_path_for_variant(variant_id=int(variant_id), icon_path=path)
            con.commit()
        finally:
            con.close()

    def latest_cycle_number(self, *, league: str) -> int:
        """Return the greatest stored poll_runs.cycle_number for this league, else 0."""
        self.ensure_initialized()
        con = self._db.connect()
        try:
            row = con.execute(
                "SELECT MAX(cycle_number) AS m FROM poll_runs WHERE league = ?",
                (league,),
            ).fetchone()
            if not row or row["m"] is None:
                return 0
            return int(row["m"])
        finally:
            con.close()

    def has_any_item_polls(self, *, variant_id: int) -> bool:
        """Whether we have ever stored an `item_polls` row for this variant."""
        if int(variant_id) <= 0:
            return False
        self.ensure_initialized()
        con = self._db.connect()
        try:
            row = con.execute(
                "SELECT 1 AS one FROM item_polls WHERE item_variant_id = ? LIMIT 1",
                (int(variant_id),),
            ).fetchone()
            return bool(row)
        finally:
            con.close()

    def load_price_alert_cooldown_rows(self) -> list[tuple[int, int, float]]:
        """
        Rows (item_variant_id, last_alert_cycle, last_alert_low_mirror) for price-drop Discord cooldown.
        """
        self.ensure_initialized()
        con = self._db.connect()
        try:
            return PriceAlertCooldownRepo(con).load_all()
        finally:
            con.close()

    def upsert_price_alert_cooldown(
        self,
        *,
        variant_id: int,
        last_cycle: int,
        last_low_mirror: float,
    ) -> None:
        self.ensure_initialized()
        con = self._db.connect()
        try:
            PriceAlertCooldownRepo(con).upsert(
                item_variant_id=int(variant_id),
                last_alert_cycle=int(last_cycle),
                last_alert_low_mirror=float(last_low_mirror),
                updated_at_utc=_utc_now_iso(),
            )
            con.commit()
        finally:
            con.close()

    def sum_estimated_sales_since(self, *, variant_id: int, since_utc_iso: str) -> int:
        """
        Sum per-poll estimated sold signals (xfer + instant + non-instant online heuristic, incl. negatives)
        for rows at or after since_utc_iso. Matches UI ``estimatedSoldCount`` aggregation.
        """
        self.ensure_initialized()
        con = self._db.connect()
        try:
            row = con.execute(
                """
                SELECT COALESCE(
                    SUM(
                        COALESCE(ip.inf_confirmed_transfer, 0)
                        + COALESCE(ip.inf_likely_instant_sale, 0)
                        + COALESCE(ip.inf_likely_non_instant_online, 0)
                    ),
                    0
                ) AS total
                FROM item_polls ip
                WHERE ip.item_variant_id = ?
                  AND ip.requested_at_utc >= ?
                """,
                (int(variant_id), str(since_utc_iso)),
            ).fetchone()
            if not row or row["total"] is None:
                return 0
            return int(row["total"])
        finally:
            con.close()

    def write_poll_result(
        self,
        *,
        cycle_number: int,
        league: str,
        run_started_at_utc: str,
        requested_at_utc: str,
        divines_per_mirror: float | None,
        top_ids_limit: int | None,
        inference_fetch_cap: int | None,
        variant_id: int,
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
        inference_counts: dict[str, int],
        fetched_for_inference: int,
        listing_preview_rows: list[dict[str, Any]],
        inference_events: list[dict[str, Any]],
        inference_state: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None = None,
        late_relist_window_days: int | None = 30,
    ) -> PollWriteResult:
        """
        Store the poll run + one item poll row + listing snapshots + inference events.

        If `inference_state` is passed, it should be
        (curr_signals, pending_instant, pending_online_non_instant) and will be persisted for next cycle comparisons.
        """
        self.ensure_initialized()
        con = self._db.connect()
        try:
            polls = PollsRepo(con)
            run_id = polls.upsert_poll_run(
                cycle_number=cycle_number,
                league=league,
                started_at_utc=run_started_at_utc,
                divines_per_mirror=divines_per_mirror,
                top_ids_limit=top_ids_limit,
                inference_fetch_cap=inference_fetch_cap,
            )
            item_poll_id = polls.insert_item_poll(
                poll_run_id=run_id,
                item_variant_id=variant_id,
                requested_at_utc=requested_at_utc,
                query_id=query_id,
                total_results=total_results,
                used_results=used_results,
                unsupported_price_count=unsupported_price_count,
                mirror_count=mirror_count,
                lowest_mirror=lowest_mirror,
                median_mirror=median_mirror,
                highest_mirror=highest_mirror,
                divine_count=divine_count,
                lowest_divine=lowest_divine,
                median_divine=median_divine,
                highest_divine=highest_divine,
                inf_confirmed_transfer=int(inference_counts.get("confirmedTransfer", 0)),
                inf_likely_instant_sale=int(inference_counts.get("likelyInstantSale", 0)),
                inf_likely_non_instant_online=int(inference_counts.get("likelyNonInstantOnline", 0)),
                inf_relist_same_seller=int(inference_counts.get("relistSameSeller", 0)),
                inf_non_instant_removed=int(inference_counts.get("nonInstantRemoved", 0)),
                inf_reprice_same_seller=int(inference_counts.get("repriceSameSeller", 0)),
                inf_multi_seller_same_fingerprint=int(inference_counts.get("multiSellerSameFingerprint", 0)),
                inf_new_listing_rows=int(inference_counts.get("newListingRows", 0)),
                fetched_for_inference=int(fetched_for_inference),
            )

            # --- Late relist reconciliation (thin-market friendly) ---
            # If a (fingerprint, seller) pair reappears within the configured window, revert the
            # previously inferred sale so it no longer counts toward totals.
            win_days = int(late_relist_window_days) if late_relist_window_days is not None else 0
            win_days = max(0, min(3650, win_days))
            late_relist_events: list[dict[str, Any]] = []
            if win_days > 0:
                try:
                    dt = datetime.fromisoformat(str(requested_at_utc))
                except Exception:
                    dt = datetime.now(timezone.utc)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                min_cutoff = (dt - timedelta(days=win_days)).isoformat()

                sales_repo = SalesRepo(con)
                seen_pairs: set[tuple[str, str]] = set()
                for r in listing_preview_rows or []:
                    if not isinstance(r, dict):
                        continue
                    fp = str(r.get("fingerprint") or "").strip()
                    seller = str(r.get("sellerName") or "").strip()
                    if not fp or not seller:
                        continue
                    key = (fp, seller)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)

                    reverted_rule: str | None = None
                    updated = sales_repo.revert_latest_sale(
                        item_variant_id=int(variant_id),
                        rule="likely_instant_sale",
                        fingerprint=fp,
                        seller=seller,
                        min_occurred_at_utc=min_cutoff,
                        occurred_at_or_before_utc=str(requested_at_utc),
                        reverted_at_utc=_utc_now_iso(),
                        reverted_by_item_poll_id=int(item_poll_id),
                        reverted_reason="relist_same_seller_late",
                    )
                    if updated:
                        reverted_rule = "likely_instant_sale"
                    else:
                        updated = sales_repo.revert_latest_sale(
                            item_variant_id=int(variant_id),
                            rule="likely_non_instant_online_sale",
                            fingerprint=fp,
                            seller=seller,
                            min_occurred_at_utc=min_cutoff,
                            occurred_at_or_before_utc=str(requested_at_utc),
                            reverted_at_utc=_utc_now_iso(),
                            reverted_by_item_poll_id=int(item_poll_id),
                            reverted_reason="relist_same_seller_late",
                        )
                        if updated:
                            reverted_rule = "likely_non_instant_online_sale"

                    if updated and reverted_rule:
                        if reverted_rule == "likely_instant_sale":
                            inference_counts["likelyInstantSale"] = int(inference_counts.get("likelyInstantSale", 0)) - 1
                        elif reverted_rule == "likely_non_instant_online_sale":
                            inference_counts["likelyNonInstantOnline"] = int(
                                inference_counts.get("likelyNonInstantOnline", 0)
                            ) - 1
                        late_relist_events.append(
                            {
                                "rule": "relist_same_seller_late",
                                "revertsSaleRule": reverted_rule,
                                "windowDays": int(win_days),
                                "itemKey": None,
                                "fingerprint": fp,
                                "seller": seller,
                                "cycle": int(cycle_number),
                            }
                        )

                if late_relist_events:
                    # Expose in persisted inference_events and UI.
                    inference_events = list(inference_events or []) + late_relist_events
                    # Bump the per-poll relist count for visibility and persist the net delta update.
                    inference_counts = dict(inference_counts or {})
                    inference_counts["relistSameSeller"] = int(inference_counts.get("relistSameSeller", 0)) + len(
                        late_relist_events
                    )
                    # Keep the DB's item_polls summary consistent with what we expose in payloads.
                    con.execute(
                        (
                            "UPDATE item_polls SET inf_likely_instant_sale = ?, "
                            "inf_likely_non_instant_online = ?, inf_relist_same_seller = ? WHERE id = ?"
                        ),
                        (
                            int(inference_counts.get("likelyInstantSale", 0)),
                            int(inference_counts.get("likelyNonInstantOnline", 0)),
                            int(inference_counts["relistSameSeller"]),
                            int(item_poll_id),
                        ),
                    )

            polls.replace_listing_snapshots(item_poll_id=item_poll_id, rows=listing_preview_rows)
            polls.replace_inference_events(item_poll_id=item_poll_id, events=inference_events)
            self._record_sales_from_inference_events(
                con,
                item_poll_id=item_poll_id,
                item_variant_id=variant_id,
                occurred_at_utc=requested_at_utc,
                inference_events=inference_events,
            )

            if inference_state is not None:
                curr_signals, pending_inst, pending_on = inference_state
                inf_repo = InferenceStateRepo(con)
                inf_repo.save_state(
                    item_variant_id=variant_id,
                    cycle=cycle_number,
                    curr_signals=curr_signals,
                    pending_instant=pending_inst,
                    pending_online=pending_on,
                )

            con.commit()
            return PollWriteResult(
                item_poll_id=int(item_poll_id),
                inference_counts=dict(inference_counts),
                inference_events=list(inference_events or []),
            )
        finally:
            con.close()

    def _record_sales_from_inference_events(
        self,
        con,
        *,
        item_poll_id: int,
        item_variant_id: int,
        occurred_at_utc: str,
        inference_events: list[dict[str, Any]],
    ) -> None:
        """
        Persist inferred sales into `sales` for every rule that counts toward estimated sales:
        - confirmed_transfer (+1)
        - likely_instant_sale (+1)
        - likely_non_instant_online_sale (+1, rule 4b)

        On relist (rule 3), revert the latest matching `likely_instant_sale` or
        `likely_non_instant_online_sale` row according to the engine event's `revertsSaleRule`.
        """
        repo = SalesRepo(con)
        now = _utc_now_iso()
        for ev in inference_events or []:
            if not isinstance(ev, dict):
                continue
            rule = str(ev.get("rule") or "")
            if rule == "relist_same_seller_late":
                # Late relist reconciliation is handled earlier in `write_poll_result` so it can apply
                # a rolling time window. Keep this rule here only for persistence/visibility.
                continue
            if rule == "relist_same_seller":
                seller = str(ev.get("seller") or "").strip()
                fp = str(ev.get("fingerprint") or "").strip()
                rsr = str(ev.get("revertsSaleRule") or "likely_instant_sale").strip()
                if rsr not in {"likely_instant_sale", "likely_non_instant_online_sale"}:
                    rsr = "likely_instant_sale"
                if seller and fp:
                    repo.revert_latest_sale(
                        item_variant_id=item_variant_id,
                        rule=rsr,
                        fingerprint=fp,
                        seller=seller,
                        occurred_at_or_before_utc=occurred_at_utc,
                        reverted_at_utc=now,
                        reverted_by_item_poll_id=item_poll_id,
                        reverted_reason="relist_same_seller",
                    )
                continue

            if rule not in {
                "confirmed_transfer",
                "likely_instant_sale",
                "likely_non_instant_online_sale",
            }:
                continue

            fp = ev.get("fingerprint")
            fingerprint = str(fp) if isinstance(fp, str) and fp.strip() else None

            if rule == "confirmed_transfer":
                seller = str(ev.get("from_seller") or "").strip()
                buyer = str(ev.get("to_seller") or "").strip() or None
                if not seller:
                    continue
                price_amount = ev.get("fromPriceAmount")
                price_currency = ev.get("fromPriceCurrency")
                mirror_equiv = ev.get("fromMirrorEquiv")
            else:
                # likely_instant_sale, likely_non_instant_online_sale
                seller = str(ev.get("seller") or "").strip()
                buyer = None
                if not seller:
                    continue
                price_amount = ev.get("priceAmount")
                price_currency = ev.get("priceCurrency")
                mirror_equiv = ev.get("mirrorEquiv")

            try:
                pa = float(price_amount) if price_amount is not None else None
            except Exception:
                pa = None
            try:
                me = float(mirror_equiv) if mirror_equiv is not None else None
            except Exception:
                me = None

            repo.insert_sale(
                item_poll_id=item_poll_id,
                item_variant_id=item_variant_id,
                occurred_at_utc=occurred_at_utc,
                recorded_at_utc=now,
                rule=rule,
                fingerprint=fingerprint,
                seller=seller,
                buyer=buyer,
                price_amount=pa,
                price_currency=str(price_currency) if isinstance(price_currency, str) else None,
                mirror_equiv=me,
                quantity=1,
            )

