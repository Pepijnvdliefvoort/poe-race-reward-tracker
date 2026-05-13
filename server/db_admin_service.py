from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storage.db import Database


_SAFE_SQL_PREFIX_RE = re.compile(r"^\s*(?:--[^\n]*\n|\s|/\*[\s\S]*?\*/)*", re.MULTILINE)


def _first_keyword(sql: str) -> str:
    s = (sql or "").strip()
    if not s:
        return ""
    # Drop leading SQL comments (very small best-effort).
    s = _SAFE_SQL_PREFIX_RE.sub("", s).lstrip()
    m = re.match(r"^([A-Za-z_]+)", s)
    return (m.group(1) if m else "").lower()


def _is_allowed_statement(sql: str) -> bool:
    """
    Admin DB explorer should be safe-by-default. Allow introspection + read-only queries.
    - select / with: read queries
    - pragma: sqlite introspection
    - explain: query plans
    """
    kw = _first_keyword(sql)
    return kw in {"select", "with", "pragma", "explain"}


def _sqlite_identifier(name: str) -> str:
    """
    Quote a sqlite identifier defensively (table/column/index name).
    This is not a substitute for parameters, but identifiers can't be parameterized.
    """
    s = str(name or "")
    return '"' + s.replace('"', '""') + '"'


def db_overview(*, root_dir: Path) -> dict[str, Any]:
    db = Database(root_dir=root_dir)
    db.ensure_initialized()
    con = db.connect()
    try:
        rows = con.execute("PRAGMA database_list").fetchall()
        dbs = [{"seq": int(r[0]), "name": str(r[1]), "file": str(r[2])} for r in rows]
        return {"ok": True, "dbPath": str(db.path), "databases": dbs}
    finally:
        con.close()


def list_tables(*, root_dir: Path) -> dict[str, Any]:
    db = Database(root_dir=root_dir)
    db.ensure_initialized()
    con = db.connect()
    try:
        rows = con.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table','view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        return {"ok": True, "tables": [{"name": str(r["name"]), "type": str(r["type"])} for r in rows]}
    finally:
        con.close()


def table_details(*, root_dir: Path, name: str) -> dict[str, Any]:
    tname = (name or "").strip()
    if not tname:
        return {"ok": False, "error": "Missing table name"}

    db = Database(root_dir=root_dir)
    db.ensure_initialized()
    con = db.connect()
    try:
        # Validate exists
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE (type IN ('table','view')) AND name = ? LIMIT 1",
            (tname,),
        ).fetchone()
        if not exists:
            return {"ok": False, "error": f"Table not found: {tname}"}

        cols = con.execute(f"PRAGMA table_info({_sqlite_identifier(tname)})").fetchall()
        col_payload = [
            {
                "cid": int(r["cid"]),
                "name": str(r["name"]),
                "type": str(r["type"] or ""),
                "notnull": int(r["notnull"] or 0) == 1,
                "dflt_value": r["dflt_value"],
                "pk": int(r["pk"] or 0) == 1,
            }
            for r in cols
        ]

        idx_rows = con.execute(f"PRAGMA index_list({_sqlite_identifier(tname)})").fetchall()
        indexes: list[dict[str, Any]] = []
        for r in idx_rows:
            idx_name = str(r["name"])
            info_rows = con.execute(f"PRAGMA index_info({_sqlite_identifier(idx_name)})").fetchall()
            indexes.append(
                {
                    "name": idx_name,
                    "unique": int(r["unique"] or 0) == 1,
                    "origin": str(r["origin"] or ""),
                    "partial": int(r["partial"] or 0) == 1,
                    "columns": [str(ir["name"]) for ir in info_rows if ir["name"] is not None],
                }
            )

        fk_rows = con.execute(f"PRAGMA foreign_key_list({_sqlite_identifier(tname)})").fetchall()
        foreign_keys = [
            {
                "id": int(r["id"]),
                "seq": int(r["seq"]),
                "table": str(r["table"] or ""),
                "from": str(r["from"] or ""),
                "to": str(r["to"] or ""),
                "on_update": str(r["on_update"] or ""),
                "on_delete": str(r["on_delete"] or ""),
                "match": str(r["match"] or ""),
            }
            for r in fk_rows
        ]

        return {
            "ok": True,
            "name": tname,
            "columns": col_payload,
            "indexes": indexes,
            "foreignKeys": foreign_keys,
        }
    finally:
        con.close()


def er_schema(*, root_dir: Path) -> dict[str, Any]:
    """
    Return enough schema info to build a lightweight ER diagram client-side.
    """
    db = Database(root_dir=root_dir)
    db.ensure_initialized()
    con = db.connect()
    try:
        tables = con.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        out_tables: list[dict[str, Any]] = []
        for r in tables:
            tname = str(r["name"])
            cols = con.execute(f"PRAGMA table_info({_sqlite_identifier(tname)})").fetchall()
            col_payload = [
                {
                    "name": str(c["name"]),
                    "type": str(c["type"] or ""),
                    "pk": int(c["pk"] or 0) == 1,
                    "notnull": int(c["notnull"] or 0) == 1,
                }
                for c in cols
            ]
            fk_rows = con.execute(f"PRAGMA foreign_key_list({_sqlite_identifier(tname)})").fetchall()
            fk_payload = [
                {
                    "table": str(fk["table"] or ""),
                    "from": str(fk["from"] or ""),
                    "to": str(fk["to"] or ""),
                }
                for fk in fk_rows
            ]
            out_tables.append({"name": tname, "columns": col_payload, "foreignKeys": fk_payload})
        return {"ok": True, "tables": out_tables}
    finally:
        con.close()


def preview_table(*, root_dir: Path, name: str, limit: int = 100) -> dict[str, Any]:
    tname = (name or "").strip()
    if not tname:
        return {"ok": False, "error": "Missing table name"}
    limit = max(1, min(int(limit or 100), 1000))

    db = Database(root_dir=root_dir)
    db.ensure_initialized()
    con = db.connect()
    try:
        sql = f"SELECT * FROM {_sqlite_identifier(tname)} LIMIT ?"
        cur = con.execute(sql, (limit,))
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
        out_rows: list[dict[str, Any]] = []
        for r in rows:
            row_obj: dict[str, Any] = {}
            for c in cols:
                v = r[c] if c in r.keys() else None
                row_obj[c] = v
            out_rows.append(row_obj)
        return {"ok": True, "name": tname, "columns": cols, "rows": out_rows, "limit": limit}
    finally:
        con.close()


@dataclass(frozen=True)
class QueryResult:
    ok: bool
    columns: list[str]
    rows: list[dict[str, Any]]
    error: str | None = None
    meta: dict[str, Any] | None = None


def run_query(*, root_dir: Path, sql: str, limit: int = 200) -> dict[str, Any]:
    statement = (sql or "").strip()
    if not statement:
        return {"ok": False, "error": "Missing sql"}
    if not _is_allowed_statement(statement):
        return {"ok": False, "error": "Only read-only SQL is allowed here (SELECT / WITH / PRAGMA / EXPLAIN)."}

    limit = max(1, min(int(limit or 200), 5000))

    db = Database(root_dir=root_dir)
    db.ensure_initialized()
    con = db.connect()
    try:
        t0 = time.perf_counter()
        cur = con.execute(statement)
        cols = [d[0] for d in (cur.description or [])]
        if cols:
            rows = cur.fetchmany(limit)
            out_rows: list[dict[str, Any]] = []
            for r in rows:
                row_obj: dict[str, Any] = {}
                # sqlite3.Row supports keys()
                for c in cols:
                    try:
                        row_obj[c] = r[c]
                    except Exception:  # noqa: BLE001
                        row_obj[c] = None
                out_rows.append(row_obj)
        else:
            out_rows = []
        elapsed_ms = int(round((time.perf_counter() - t0) * 1000))
        return {
            "ok": True,
            "columns": cols,
            "rows": out_rows,
            "meta": {"elapsedMs": elapsed_ms, "rowCount": len(out_rows), "limit": limit},
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()

