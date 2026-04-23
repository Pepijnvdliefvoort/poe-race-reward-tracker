from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .schema import (
    SCHEMA_VERSION,
    migration_001_initial,
    migration_002_app_config,
    migration_003_visitors,
    migration_004_sales,
    migration_005_sales_reverts,
    migration_006_inference_price_state,
    migration_007_price_alert_cooldown,
    migration_008_non_instant_online_inference,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DbPaths:
    root_dir: Path

    @property
    def db_path(self) -> Path:
        return self.root_dir / "data" / "market.db"


class Database:
    """
    Small wrapper around sqlite3 with:
    - WAL mode
    - foreign key enforcement
    - schema migrations
    - thread-safe connection creation (one connection per call)
    """

    def __init__(self, root_dir: Path) -> None:
        self._paths = DbPaths(root_dir=root_dir)
        self._init_lock = threading.Lock()
        self._initialized = False

    @property
    def path(self) -> Path:
        return self._paths.db_path

    def connect(self) -> sqlite3.Connection:
        self.ensure_initialized()
        con = sqlite3.connect(self.path, timeout=30.0)
        con.row_factory = sqlite3.Row
        # Safety + concurrency defaults.
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute("PRAGMA journal_mode = WAL;")
        con.execute("PRAGMA synchronous = NORMAL;")
        con.execute("PRAGMA busy_timeout = 30000;")
        return con

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path, timeout=30.0)
            try:
                con.execute("PRAGMA foreign_keys = ON;")
                con.execute("PRAGMA journal_mode = WAL;")
                con.execute("PRAGMA synchronous = NORMAL;")
                self._apply_migrations(con)
                con.commit()
            finally:
                con.close()
            self._initialized = True

    def _applied_versions(self, con: sqlite3.Connection) -> set[int]:
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY NOT NULL, applied_at_utc TEXT NOT NULL)"
        )
        rows = con.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        return {int(r[0]) for r in rows}

    def _apply_migrations(self, con: sqlite3.Connection) -> None:
        applied = self._applied_versions(con)
        migrations: list[tuple[int, str]] = [
            (1, migration_001_initial()),
            (2, migration_002_app_config()),
            (3, migration_003_visitors()),
            (4, migration_004_sales()),
            (5, migration_005_sales_reverts()),
            (6, migration_006_inference_price_state()),
            (7, migration_007_price_alert_cooldown()),
            (8, migration_008_non_instant_online_inference()),
        ]

        for version, sql in migrations:
            if version in applied:
                continue
            if version == 5:
                self._migration_005_sales_reverts(con)
            elif version == 6:
                self._migration_006_inference_price_state(con)
            elif version == 8:
                self._migration_008_non_instant_online_inference(con)
            elif sql.strip():
                con.executescript(sql)
            con.execute(
                "INSERT INTO schema_migrations(version, applied_at_utc) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )

        if max(applied | {0}) > SCHEMA_VERSION:
            raise RuntimeError(f"DB schema version is newer than app supports: {max(applied)} > {SCHEMA_VERSION}")

    def _migration_005_sales_reverts(self, con: sqlite3.Connection) -> None:
        # Idempotent migration: older DBs need these columns; newer DBs may already have them.
        rows = con.execute("PRAGMA table_info(sales)").fetchall()
        # During migrations we use a plain sqlite3 connection (row_factory not set),
        # so PRAGMA rows are tuples: (cid, name, type, notnull, dflt_value, pk).
        cols = {str((r["name"] if isinstance(r, sqlite3.Row) else r[1])) for r in rows}
        if "reverted_at_utc" not in cols:
            con.execute("ALTER TABLE sales ADD COLUMN reverted_at_utc TEXT")
        if "reverted_by_item_poll_id" not in cols:
            con.execute(
                "ALTER TABLE sales ADD COLUMN reverted_by_item_poll_id INTEGER REFERENCES item_polls(id) ON DELETE SET NULL"
            )
        if "reverted_reason" not in cols:
            con.execute("ALTER TABLE sales ADD COLUMN reverted_reason TEXT")

    def _migration_006_inference_price_state(self, con: sqlite3.Connection) -> None:
        """
        Persist last-seen listing price (amount + currency) for inference state rows so
        downstream inference events / Discord notifications can display prices even when
        the currency can't be converted to mirror-equivalent.
        """
        # inference_state_signals: add price fields
        sig_cols = {
            str((r["name"] if isinstance(r, sqlite3.Row) else r[1]))
            for r in con.execute("PRAGMA table_info(inference_state_signals)").fetchall()
        }
        if "price_amount" not in sig_cols:
            con.execute("ALTER TABLE inference_state_signals ADD COLUMN price_amount REAL")
        if "price_currency" not in sig_cols:
            con.execute("ALTER TABLE inference_state_signals ADD COLUMN price_currency TEXT")

        # inference_state_pending: add price fields + mirror_equiv (so events can carry it forward)
        pend_cols = {
            str((r["name"] if isinstance(r, sqlite3.Row) else r[1]))
            for r in con.execute("PRAGMA table_info(inference_state_pending)").fetchall()
        }
        if "mirror_equiv" not in pend_cols:
            con.execute("ALTER TABLE inference_state_pending ADD COLUMN mirror_equiv REAL")
        if "price_amount" not in pend_cols:
            con.execute("ALTER TABLE inference_state_pending ADD COLUMN price_amount REAL")
        if "price_currency" not in pend_cols:
            con.execute("ALTER TABLE inference_state_pending ADD COLUMN price_currency TEXT")

    def _migration_008_non_instant_online_inference(self, con: sqlite3.Connection) -> None:
        poll_cols = {
            str((r["name"] if isinstance(r, sqlite3.Row) else r[1]))
            for r in con.execute("PRAGMA table_info(item_polls)").fetchall()
        }
        if "inf_likely_non_instant_online" not in poll_cols:
            con.execute(
                "ALTER TABLE item_polls ADD COLUMN inf_likely_non_instant_online INTEGER NOT NULL DEFAULT 0"
            )

        sig_cols = {
            str((r["name"] if isinstance(r, sqlite3.Row) else r[1]))
            for r in con.execute("PRAGMA table_info(inference_state_signals)").fetchall()
        }
        if "seller_online" not in sig_cols:
            con.execute("ALTER TABLE inference_state_signals ADD COLUMN seller_online INTEGER NOT NULL DEFAULT 0")

        pend_cols = {
            str((r["name"] if isinstance(r, sqlite3.Row) else r[1]))
            for r in con.execute("PRAGMA table_info(inference_state_pending)").fetchall()
        }
        if "pending_kind" not in pend_cols:
            con.execute(
                "ALTER TABLE inference_state_pending ADD COLUMN pending_kind TEXT NOT NULL DEFAULT 'instant'"
            )


def execute_many(con: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    con.executemany(sql, list(rows))

