from __future__ import annotations

import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
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


# Keep parts safely below lower-tier Discord webhook limits to avoid HTTP 413.
DISCORD_SAFE_PART_SIZE_BYTES = 7 * 1024 * 1024  # 7 MiB


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


def _compact_sqlite_db(src_db_path: Path, dst_db_path: Path) -> bool:
    """
    Compact a snapshot DB to reduce size (helpful for Discord attachment limits).

    Uses `VACUUM INTO` (SQLite >= 3.27). If unavailable/fails, returns False.
    """
    dst_db_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_db_path.exists():
        dst_db_path.unlink()

    conn = sqlite3.connect(str(src_db_path), timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout = 60000;")
        conn.execute("VACUUM INTO ?;", (str(dst_db_path),))
        return True
    except Exception:
        try:
            if dst_db_path.exists():
                dst_db_path.unlink()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _zip_file(src_path: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    # Prefer stronger compression to fit Discord webhook attachment limits.
    # LZMA is slower but usually produces smaller archives for SQLite databases.
    compression = getattr(zipfile, "ZIP_LZMA", zipfile.ZIP_DEFLATED)
    compresslevel = 9 if compression == zipfile.ZIP_DEFLATED else None
    kwargs: dict = {"compression": compression}
    if compresslevel is not None:
        kwargs["compresslevel"] = compresslevel
    with zipfile.ZipFile(zip_path, "w", **kwargs) as zf:
        zf.write(src_path, arcname=src_path.name)


def _discord_webhook_post_file(
    *,
    webhook_url: str,
    content: str,
    file_path: Path,
    timeout: float,
) -> requests.Response:
    """
    Post a file attachment to a Discord webhook.

    Important: do NOT reuse the poller's shared requests.Session here, because the poller
    sets a default `Content-Type: application/json` header for PoE API calls, which breaks
    Discord multipart uploads (Discord returns HTTP 400 code 50109).
    """
    # Keep headers minimal so `requests` can set the correct multipart boundary.
    headers = {"Accept": "*/*"}
    with file_path.open("rb") as fh:
        return requests.post(
            webhook_url,
            headers=headers,
            data={"content": content},
            files={"file": (file_path.name, fh, "application/octet-stream")},
            timeout=timeout,
        )


def _split_file_into_parts(*, src_path: Path, part_size_bytes: int) -> list[Path]:
    if part_size_bytes <= 0:
        raise ValueError("part_size_bytes must be > 0")

    src_size = int(src_path.stat().st_size)
    if src_size <= part_size_bytes:
        return [src_path]

    total_parts = int(ceil(src_size / float(part_size_bytes)))
    parts: list[Path] = []
    with src_path.open("rb") as in_fh:
        for idx in range(total_parts):
            part_name = f"{src_path.name}.part{idx + 1:03d}"
            part_path = src_path.with_name(part_name)
            with part_path.open("wb") as out_fh:
                out_fh.write(in_fh.read(part_size_bytes))
            parts.append(part_path)
    return parts


def _raise_http_error_with_body(*, resp: requests.Response, exc: Exception, body_limit: int) -> None:
    text = ""
    try:
        text = (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        text = ""
    if len(text) > body_limit:
        text = text[:body_limit] + "…"
    status = getattr(resp, "status_code", None)
    suffix = f" Body: {text}" if text else ""
    raise requests.HTTPError(f"{exc} (HTTP {status}).{suffix}") from exc


def _upload_file_or_parts_to_discord(
    *,
    webhook_url: str,
    content_prefix: str,
    file_path: Path,
    timeout: float,
    log: callable,
) -> None:
    file_size = int(file_path.stat().st_size)
    if file_size <= DISCORD_SAFE_PART_SIZE_BYTES:
        resp = _discord_webhook_post_file(
            webhook_url=webhook_url,
            content=content_prefix,
            file_path=file_path,
            timeout=timeout,
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            _raise_http_error_with_body(resp=resp, exc=exc, body_limit=1200)
        return

    parts = _split_file_into_parts(src_path=file_path, part_size_bytes=DISCORD_SAFE_PART_SIZE_BYTES)
    total_parts = len(parts)
    log(
        "warn",
        (
            f"DB export archive is {file_size / (1024 * 1024):.2f} MiB; "
            f"uploading as {total_parts} parts (~{DISCORD_SAFE_PART_SIZE_BYTES / (1024 * 1024):.2f} MiB each)."
        ),
    )
    try:
        for idx, part_path in enumerate(parts, start=1):
            content = (
                f"{content_prefix}"
                f"\nPart {idx}/{total_parts}: `{part_path.name}`"
                "\nReassemble by concatenating parts in order."
            )
            resp = _discord_webhook_post_file(
                webhook_url=webhook_url,
                content=content,
                file_path=part_path,
                timeout=timeout,
            )
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                _raise_http_error_with_body(resp=resp, exc=exc, body_limit=1200)
    finally:
        for part_path in parts:
            if part_path != file_path:
                try:
                    part_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass


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
    compacted_path = exports_dir / f"market.{stamp}.compact.db"
    zip_path = exports_dir / f"market.{stamp}.db.zip"

    try:
        _snapshot_sqlite_db(storage.db_path, snapshot_path)
        use_path = snapshot_path
        if _compact_sqlite_db(snapshot_path, compacted_path):
            use_path = compacted_path
        _zip_file(use_path, zip_path)

        size_bytes = int(zip_path.stat().st_size)
        size_mb = size_bytes / (1024 * 1024)
        ts = int(now_utc.timestamp())
        content = f"Daily DB export at <t:{ts}:F>, `{zip_path.name}` ({size_mb:.2f} MiB)."
        _upload_file_or_parts_to_discord(
            webhook_url=webhook_url,
            content_prefix=content,
            file_path=zip_path,
            timeout=90.0,
            log=log,
        )

        storage.set_config(
            key="db_export",
            value={
                "last_uploaded_at_utc": now_utc.isoformat(),
                "last_uploaded_local_date": today_local.isoformat(),
                "tz_offset_minutes": int(cfg.tz_offset_minutes),
                "schedule_hour": int(cfg.schedule_hour),
                "schedule_minute": int(cfg.schedule_minute),
                "last_uploaded_file": zip_path.name,
                "last_uploaded_size_bytes": size_bytes,
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
    compacted_path = exports_dir / f"market.{stamp}.compact.db"
    zip_path = exports_dir / f"market.{stamp}.db.zip"

    _snapshot_sqlite_db(storage.db_path, snapshot_path)
    use_path = snapshot_path
    if _compact_sqlite_db(snapshot_path, compacted_path):
        use_path = compacted_path
    _zip_file(use_path, zip_path)

    size_bytes = int(zip_path.stat().st_size)
    size_mb = size_bytes / (1024 * 1024)
    ts = int(now_utc.timestamp())
    content = f"Manual DB export at <t:{ts}:F>, `{zip_path.name}` ({size_mb:.2f} MiB)."
    try:
        _upload_file_or_parts_to_discord(
            webhook_url=webhook_url,
            content_prefix=content,
            file_path=zip_path,
            timeout=90.0,
            log=log,
        )
    except requests.HTTPError as exc:
        return {"ok": False, "error": f"Discord upload failed: {exc}"}
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
