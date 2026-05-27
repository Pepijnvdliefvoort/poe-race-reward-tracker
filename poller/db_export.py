from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
    retention_days: int = 45


# Discord allows 25 MiB attachments per message; stay slightly under for multipart overhead.
DISCORD_MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024  # 24 MiB


def _cleanup_old_exports(*, exports_dir: Path, retention_days: int, log: callable) -> None:
    if retention_days < 0 or not exports_dir.exists():
        return
    cutoff = _utc_now() - timedelta(days=int(retention_days))
    removed = 0
    for path in exports_dir.iterdir():
        if not path.is_file():
            continue
        if not path.name.startswith("market."):
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except Exception:
            continue
        if mtime >= cutoff:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except Exception:
            continue
    if removed > 0:
        log("cycle", f"DB export retention removed {removed} old file(s) (>{retention_days}d).")


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


def _webhook_execute_url(
    webhook_url: str,
    *,
    thread_id: str | None = None,
    wait: bool = False,
) -> str:
    parsed = urlparse(webhook_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if thread_id:
        query["thread_id"] = thread_id
    if wait:
        query["wait"] = "true"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _thread_id_from_webhook_message_response(resp: requests.Response) -> str | None:
    """Forum thread id is the ``channel_id`` on the starter message when ``wait=true``."""
    if not resp.ok:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    channel_id = data.get("channel_id")
    if channel_id is None:
        return None
    return str(channel_id)


def _forum_thread_name(*, local_date: date) -> str:
    """Discord thread_name limit is 100 characters."""
    return f"Daily backup · {local_date.isoformat()}"[:100]


def _discord_webhook_post_message(
    *,
    webhook_url: str,
    content: str = "",
    thread_name: str | None = None,
    thread_id: str | None = None,
    wait: bool = False,
    timeout: float,
) -> requests.Response:
    url = _webhook_execute_url(webhook_url, thread_id=thread_id, wait=wait)
    payload: dict = {}
    if content.strip():
        payload["content"] = content
    if thread_name:
        payload["thread_name"] = thread_name[:100]
    headers = {"Accept": "*/*"}
    return requests.post(url, headers=headers, json=payload, timeout=timeout)


def _ensure_backup_forum_thread(
    *,
    webhook_url: str,
    local_date: date,
    starter_content: str,
    state: dict,
) -> str:
    today_key = local_date.isoformat()
    stored_date = str(state.get("forum_thread_local_date", "")).strip()
    thread_id = str(state.get("forum_thread_id", "")).strip()
    if stored_date == today_key and thread_id:
        return thread_id

    starter = _discord_webhook_post_message(
        webhook_url=webhook_url,
        content=starter_content,
        thread_name=_forum_thread_name(local_date=local_date),
        wait=True,
        timeout=30.0,
    )
    starter.raise_for_status()
    new_id = _thread_id_from_webhook_message_response(starter)
    if new_id is None:
        raise RuntimeError(
            "Daily backup starter posted but Discord did not return a forum thread id; "
            "ensure the webhook targets a forum or media channel."
        )
    return new_id


def _discord_webhook_post_file(
    *,
    webhook_url: str,
    content: str,
    file_path: Path,
    timeout: float,
    thread_id: str | None = None,
) -> requests.Response:
    """
    Post a file attachment to a Discord webhook.

    Important: do NOT reuse the poller's shared requests.Session here, because the poller
    sets a default `Content-Type: application/json` header for PoE API calls, which breaks
    Discord multipart uploads (Discord returns HTTP 400 code 50109).
    """
    url = _webhook_execute_url(webhook_url, thread_id=thread_id)
    # Keep headers minimal so `requests` can set the correct multipart boundary.
    headers = {"Accept": "*/*"}
    with file_path.open("rb") as fh:
        return requests.post(
            url,
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
    thread_id: str,
) -> None:
    file_size = int(file_path.stat().st_size)
    if file_size <= DISCORD_MAX_ATTACHMENT_BYTES:
        resp = _discord_webhook_post_file(
            webhook_url=webhook_url,
            content=content_prefix,
            file_path=file_path,
            timeout=timeout,
            thread_id=thread_id,
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            _raise_http_error_with_body(resp=resp, exc=exc, body_limit=1200)
        return

    parts = _split_file_into_parts(src_path=file_path, part_size_bytes=DISCORD_MAX_ATTACHMENT_BYTES)
    total_parts = len(parts)
    log(
        "warn",
        (
            f"DB export archive is {file_size / (1024 * 1024):.2f} MiB; "
            f"uploading as {total_parts} parts (~{DISCORD_MAX_ATTACHMENT_BYTES / (1024 * 1024):.0f} MiB each)."
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
                thread_id=thread_id,
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
        thread_id = _ensure_backup_forum_thread(
            webhook_url=webhook_url,
            local_date=today_local,
            starter_content=f"**Daily DB backup** — <t:{ts}:F>",
            state=state,
        )
        content = f"`{zip_path.name}` ({size_mb:.2f} MiB)"
        _upload_file_or_parts_to_discord(
            webhook_url=webhook_url,
            content_prefix=content,
            file_path=zip_path,
            timeout=90.0,
            log=log,
            thread_id=thread_id,
        )

        storage.set_config(
            key="db_export",
            value={
                "last_uploaded_at_utc": now_utc.isoformat(),
                "last_uploaded_local_date": today_local.isoformat(),
                "forum_thread_id": thread_id,
                "forum_thread_local_date": today_local.isoformat(),
                "tz_offset_minutes": int(cfg.tz_offset_minutes),
                "schedule_hour": int(cfg.schedule_hour),
                "schedule_minute": int(cfg.schedule_minute),
                "retention_days": int(cfg.retention_days),
                "last_uploaded_file": zip_path.name,
                "last_uploaded_size_bytes": size_bytes,
            },
        )
        _cleanup_old_exports(exports_dir=exports_dir, retention_days=int(cfg.retention_days), log=log)
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
    today_local = now_local.date()
    state = storage.get_config(key="db_export") or {}
    try:
        thread_id = _ensure_backup_forum_thread(
            webhook_url=webhook_url,
            local_date=today_local,
            starter_content=f"**Daily DB backup** — <t:{ts}:F>",
            state=state,
        )
        content = f"Manual export — `{zip_path.name}` ({size_mb:.2f} MiB)"
        _upload_file_or_parts_to_discord(
            webhook_url=webhook_url,
            content_prefix=content,
            file_path=zip_path,
            timeout=90.0,
            log=log,
            thread_id=thread_id,
        )
    except requests.HTTPError as exc:
        return {"ok": False, "error": f"Discord upload failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Discord upload failed: {exc}"}

    storage.set_config(
        key="db_export",
        value={
            "last_uploaded_at_utc": now_utc.isoformat(),
            "last_uploaded_local_date": today_local.isoformat(),
            "forum_thread_id": thread_id,
            "forum_thread_local_date": today_local.isoformat(),
            "tz_offset_minutes": int(cfg.tz_offset_minutes),
            "schedule_hour": int(cfg.schedule_hour),
            "schedule_minute": int(cfg.schedule_minute),
            "retention_days": int(cfg.retention_days),
            "last_uploaded_file": zip_path.name,
            "last_uploaded_size_bytes": size_bytes,
            "last_uploaded_mode": "manual",
        },
    )
    _cleanup_old_exports(exports_dir=exports_dir, retention_days=int(cfg.retention_days), log=log)
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
