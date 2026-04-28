from __future__ import annotations

import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from storage.service import StorageService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _tz_from_offset_minutes(offset_minutes: int) -> timezone:
    return timezone(timedelta(minutes=int(offset_minutes)))


def _parse_utc_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class DbExportConfig:
    webhook_url: str
    tz_offset_minutes: int = 120  # GMT+2 default
    schedule_hour: int = 12
    schedule_minute: int = 0


def _snapshot_sqlite_db(src_db_path: Path, dst_db_path: Path) -> None:
    """
    Create a consistent snapshot of a live SQLite DB (incl. WAL mode) using the backup API.
    """
    dst_db_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_db_path.exists():
        dst_db_path.unlink()

    src = sqlite3.connect(str(src_db_path), timeout=30.0)
    dst = sqlite3.connect(str(dst_db_path), timeout=30.0)
    try:
        src.execute("PRAGMA busy_timeout = 30000;")
        dst.execute("PRAGMA journal_mode = DELETE;")
        src.backup(dst)
        dst.commit()
    finally:
        try:
            dst.close()
        finally:
            src.close()


def _zip_file(src_path: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(src_path, arcname=src_path.name)


def maybe_export_db_to_discord(
    *,
    storage: StorageService,
    session: requests.Session,
    cfg: DbExportConfig,
    log: callable,
) -> None:
    """
    Upload a daily DB snapshot to a Discord webhook as a file attachment.

    The "last uploaded" timestamp is stored in SQLite config key `db_export`.
    """
    webhook_url = (cfg.webhook_url or "").strip()
    if not webhook_url:
        return

    state = storage.get_config(key="db_export") or {}
    tz = _tz_from_offset_minutes(cfg.tz_offset_minutes)
    now_utc = _utc_now()
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()
    after_schedule = (now_local.hour, now_local.minute) >= (int(cfg.schedule_hour), int(cfg.schedule_minute))

    last_uploaded_local_date = str(state.get("last_uploaded_local_date", "")).strip()
    already_uploaded_today = last_uploaded_local_date == today_local.isoformat()
    if already_uploaded_today or (not after_schedule):
        return

    root_dir = storage.db_path.parent.parent
    exports_dir = root_dir / "storage" / "exports"
    stamp = now_utc.strftime("%Y%m%d")
    snapshot_path = exports_dir / f"market.{stamp}.db"
    zip_path = exports_dir / f"market.{stamp}.db.zip"

    try:
        _snapshot_sqlite_db(storage.db_path, snapshot_path)
        _zip_file(snapshot_path, zip_path)

        size_mb = zip_path.stat().st_size / (1024 * 1024)
        content = (
            f"Daily DB export (local {now_local.strftime('%Y-%m-%d %H:%M')} GMT{cfg.tz_offset_minutes/60:+.0f}), "
            f"`{zip_path.name}` ({size_mb:.2f} MiB)."
        )
        with zip_path.open("rb") as fh:
            resp = session.post(
                webhook_url,
                data={"content": content},
                files={"file": (zip_path.name, fh, "application/zip")},
                timeout=60.0,
            )
        resp.raise_for_status()

        storage.set_config(
            key="db_export",
            value={
                "last_uploaded_at_utc": now_utc.isoformat(),
                "last_uploaded_local_date": today_local.isoformat(),
                "tz_offset_minutes": int(cfg.tz_offset_minutes),
                "schedule_hour": int(cfg.schedule_hour),
                "schedule_minute": int(cfg.schedule_minute),
                "last_uploaded_file": zip_path.name,
                "last_uploaded_size_bytes": int(zip_path.stat().st_size),
            },
        )
        log("cycle", f"Uploaded daily DB export to Discord: {zip_path.name} ({size_mb:.2f} MiB)")
    except Exception as exc:  # noqa: BLE001
        log("warn", f"DB export to Discord failed: {exc}")


def export_db_to_discord_now(
    *,
    storage: StorageService,
    session: requests.Session,
    cfg: DbExportConfig,
    log: callable,
) -> dict:
    """
    Manual/admin-triggered DB export upload.

    Unlike `maybe_export_db_to_discord`, this bypasses the "once per day after 12:00 local"
    guard and will always attempt an upload (as long as `cfg.webhook_url` is configured).
    """
    webhook_url = (cfg.webhook_url or "").strip()
    if not webhook_url:
        return {"ok": False, "error": "Missing DB export webhook URL"}

    tz = _tz_from_offset_minutes(cfg.tz_offset_minutes)
    now_utc = _utc_now()
    now_local = now_utc.astimezone(tz)

    root_dir = storage.db_path.parent.parent
    exports_dir = root_dir / "storage" / "exports"
    stamp = now_utc.strftime("%Y%m%d")
    snapshot_path = exports_dir / f"market.{stamp}.db"
    zip_path = exports_dir / f"market.{stamp}.db.zip"

    _snapshot_sqlite_db(storage.db_path, snapshot_path)
    _zip_file(snapshot_path, zip_path)

    size_bytes = int(zip_path.stat().st_size)
    size_mb = size_bytes / (1024 * 1024)
    content = (
        f"Manual DB export (local {now_local.strftime('%Y-%m-%d %H:%M')} GMT{cfg.tz_offset_minutes/60:+.0f}), "
        f"`{zip_path.name}` ({size_mb:.2f} MiB)."
    )
    with zip_path.open("rb") as fh:
        resp = session.post(
            webhook_url,
            data={"content": content},
            files={"file": (zip_path.name, fh, "application/zip")},
            timeout=90.0,
        )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        # Discord often returns a useful JSON/body (e.g. file too large / invalid webhook).
        # Bubble that up so the admin UI can display a meaningful failure reason.
        text = ""
        try:
            text = (resp.text or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        if len(text) > 800:
            text = text[:800] + "…"
        status = getattr(resp, "status_code", None)
        return {
            "ok": False,
            "error": f"Discord upload failed (HTTP {status}): {text}" if text else f"Discord upload failed (HTTP {status}).",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Discord upload failed: {exc}"}

    storage.set_config(
        key="db_export",
        value={
            "last_uploaded_at_utc": now_utc.isoformat(),
            "last_uploaded_local_date": now_local.date().isoformat(),
            "tz_offset_minutes": int(cfg.tz_offset_minutes),
            "schedule_hour": int(cfg.schedule_hour),
            "schedule_minute": int(cfg.schedule_minute),
            "last_uploaded_file": zip_path.name,
            "last_uploaded_size_bytes": size_bytes,
            "last_uploaded_mode": "manual",
        },
    )
    log("cycle", f"Uploaded manual DB export to Discord: {zip_path.name} ({size_mb:.2f} MiB)")
    return {
        "ok": True,
        "file": zip_path.name,
        "sizeBytes": size_bytes,
        "sizeMiB": round(size_mb, 3),
        "snapshotPath": str(snapshot_path),
        "zipPath": str(zip_path),
        "uploadedAtUtc": now_utc.isoformat(),
    }
