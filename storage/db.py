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
        ]

        for version, sql in migrations:
            if version in applied:
                continue
            if version == 5:
                self._migration_005_sales_reverts(con)
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
        cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(sales)").fetchall()}
        if "reverted_at_utc" not in cols:
            con.execute("ALTER TABLE sales ADD COLUMN reverted_at_utc TEXT")
        if "reverted_by_item_poll_id" not in cols:
            con.execute(
                "ALTER TABLE sales ADD COLUMN reverted_by_item_poll_id INTEGER REFERENCES item_polls(id) ON DELETE SET NULL"
            )
        if "reverted_reason" not in cols:
            con.execute("ALTER TABLE sales ADD COLUMN reverted_reason TEXT")


def execute_many(con: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    con.executemany(sql, list(rows))

