from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage.service import StorageService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _tz_from_offset_minutes(offset_minutes: int) -> timezone:
    return timezone(timedelta(minutes=int(offset_minutes)))


def _tail(text: str, max_len: int = 1000) -> str:
    cleaned = str(text or "").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return f"...{cleaned[-max_len:]}"


def _tail_file(path: Path, max_len: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return _tail(text, max_len=max_len)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


_ACTIVE_PROC: subprocess.Popen[str] | None = None
_ACTIVE_WEEK_KEY: str | None = None
_ACTIVE_STARTED_AT_UTC: str | None = None
_ACTIVE_LOG_PATH: Path | None = None
_ACTIVE_LOCK = threading.RLock()


@dataclass(frozen=True)
class MlRetrainConfig:
    enabled: bool = True
    tz_offset_minutes: int = 120  # GMT+2 default
    schedule_weekday: int = 6  # 0=Mon ... 6=Sun
    schedule_hour: int = 3
    schedule_minute: int = 30
    timeout_seconds: int = 7200
    python_executable: str = sys.executable
    script_rel_path: str = "scripts/retrain_ml_pipeline.py"


def _lock_path(root_dir: Path) -> Path:
    return root_dir / "logs" / "ml_retrain.lock"


def _write_lock(root_dir: Path, payload: dict[str, str | int | None]) -> None:
    path = _lock_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_lock(root_dir: Path) -> None:
    try:
        _lock_path(root_dir).unlink(missing_ok=True)
    except Exception:
        pass


def _clear_active() -> None:
    global _ACTIVE_PROC, _ACTIVE_WEEK_KEY, _ACTIVE_STARTED_AT_UTC, _ACTIVE_LOG_PATH
    _ACTIVE_PROC = None
    _ACTIVE_WEEK_KEY = None
    _ACTIVE_STARTED_AT_UTC = None
    _ACTIVE_LOG_PATH = None


def _finalize_active_run(
    *,
    storage: StorageService,
    cfg: MlRetrainConfig,
    root_dir: Path,
    log: callable,
    rc: int,
) -> None:
    if _ACTIVE_PROC is None:
        return

    timeout_seconds = max(300, int(cfg.timeout_seconds))
    log_tail = _tail_file(_ACTIVE_LOG_PATH) if _ACTIVE_LOG_PATH else ""
    ok = rc == 0
    storage.set_config(
        key="ml_retrain",
        value={
            "last_attempt_at_utc": _ACTIVE_STARTED_AT_UTC,
            "last_completed_at_utc": _utc_now().isoformat(),
            "last_status": "ok" if ok else "failed",
            "last_exit_code": int(rc),
            "last_run_week_key": _ACTIVE_WEEK_KEY if ok else str((storage.get_config(key="ml_retrain") or {}).get("last_run_week_key") or ""),
            "running": False,
            "schedule_weekday": int(cfg.schedule_weekday),
            "schedule_hour": int(cfg.schedule_hour),
            "schedule_minute": int(cfg.schedule_minute),
            "tz_offset_minutes": int(cfg.tz_offset_minutes),
            "timeout_seconds": int(timeout_seconds),
            "last_log_path": str(_ACTIVE_LOG_PATH) if _ACTIVE_LOG_PATH else None,
            "last_log_tail": log_tail,
        },
    )
    _remove_lock(root_dir)
    if ok:
        log("cycle", f"Weekly ML retrain finished successfully (week={_ACTIVE_WEEK_KEY}).")
    else:
        log("warn", f"Weekly ML retrain failed with exit code {rc}. See {_ACTIVE_LOG_PATH}.")
    _clear_active()


def _watch_active_run(
    *,
    proc: subprocess.Popen[str],
    storage: StorageService,
    cfg: MlRetrainConfig,
    root_dir: Path,
    log: callable,
) -> None:
    try:
        rc = int(proc.wait())
    except Exception:
        return

    with _ACTIVE_LOCK:
        # Ignore if this process is no longer the active retrain run.
        if _ACTIVE_PROC is not proc:
            return
        _finalize_active_run(storage=storage, cfg=cfg, root_dir=root_dir, log=log, rc=rc)


def _refresh_active_run(*, storage: StorageService, cfg: MlRetrainConfig, root_dir: Path, log: callable) -> None:
    with _ACTIVE_LOCK:
        if _ACTIVE_PROC is None:
            return

        timeout_seconds = max(300, int(cfg.timeout_seconds))
        started = _ACTIVE_STARTED_AT_UTC
        if started:
            try:
                started_dt = datetime.fromisoformat(started)
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                elapsed = (_utc_now() - started_dt.astimezone(timezone.utc)).total_seconds()
            except Exception:
                elapsed = 0.0
            if elapsed > timeout_seconds and _ACTIVE_PROC.poll() is None:
                try:
                    _ACTIVE_PROC.kill()
                except Exception:
                    pass

        rc = _ACTIVE_PROC.poll()
        if rc is None:
            return
        _finalize_active_run(storage=storage, cfg=cfg, root_dir=root_dir, log=log, rc=int(rc))


def maybe_run_weekly_ml_retrain(*, storage: StorageService, cfg: MlRetrainConfig, log: callable) -> None:
    """
    Run ML retraining pipeline once per local calendar week after the configured schedule.

    State is persisted in SQLite config key `ml_retrain`.
    """
    if not cfg.enabled:
        return

    root_dir = storage.db_path.parent.parent
    _refresh_active_run(storage=storage, cfg=cfg, root_dir=root_dir, log=log)

    script_path = root_dir / str(cfg.script_rel_path)
    if not script_path.is_file():
        log("warn", f"Weekly ML retrain is enabled but script was not found: {script_path}")
        return

    state = storage.get_config(key="ml_retrain") or {}
    tz = _tz_from_offset_minutes(cfg.tz_offset_minutes)
    now_utc = _utc_now()
    now_local = now_utc.astimezone(tz)

    schedule_weekday = max(0, min(6, int(cfg.schedule_weekday)))
    days_since_schedule = (now_local.weekday() - schedule_weekday) % 7
    scheduled_date = now_local.date() - timedelta(days=days_since_schedule)
    # On the scheduled weekday itself, only fire after the scheduled time of day.
    # On any later day (retrain is overdue), fire immediately regardless of time.
    after_schedule = days_since_schedule > 0 or (now_local.hour, now_local.minute) >= (int(cfg.schedule_hour), int(cfg.schedule_minute))
    if not after_schedule:
        return

    iso_year, iso_week, _iso_weekday = scheduled_date.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    last_run_week_key = str(state.get("last_run_week_key", "") or "").strip()
    if last_run_week_key == week_key:
        return

    lock = _lock_path(root_dir)
    if lock.exists():
        try:
            lock_data = json.loads(lock.read_text(encoding="utf-8"))
        except Exception:
            lock_data = {}
        lock_pid = int(lock_data.get("pid") or 0)
        if lock_pid > 0 and _pid_alive(lock_pid):
            return
        # stale lock file from old process/crash
        _remove_lock(root_dir)

    cmd = [str(cfg.python_executable or sys.executable), str(script_path)]
    timeout_seconds = max(300, int(cfg.timeout_seconds))
    started_at_utc = _utc_now().isoformat()
    log(
        "cycle",
        (
            "Starting weekly ML retrain pipeline "
            f"(week={week_key}, local={now_local.isoformat(timespec='seconds')}, timeout={timeout_seconds}s)."
        ),
    )

    run_log_path = root_dir / "logs" / f"ml-retrain-{_utc_now().strftime('%Y%m%dT%H%M%SZ')}.log"
    run_log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_fh = run_log_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(root_dir),
            stdout=log_fh,
            stderr=log_fh,
            text=True,
            start_new_session=True,
        )
        log_fh.close()
    except Exception as exc:
        storage.set_config(
            key="ml_retrain",
            value={
                "last_attempt_at_utc": started_at_utc,
                "last_status": "failed-to-launch",
                "last_exit_code": None,
                "last_run_week_key": last_run_week_key,
                "schedule_weekday": schedule_weekday,
                "schedule_hour": int(cfg.schedule_hour),
                "schedule_minute": int(cfg.schedule_minute),
                "tz_offset_minutes": int(cfg.tz_offset_minutes),
                "timeout_seconds": timeout_seconds,
                "last_error": str(exc),
                "running": False,
            },
        )
        log("warn", f"Weekly ML retrain failed to launch: {exc}")
        return

    _write_lock(
        root_dir,
        {
            "pid": int(proc.pid),
            "week_key": week_key,
            "started_at_utc": started_at_utc,
            "log_path": str(run_log_path),
        },
    )
    storage.set_config(
        key="ml_retrain",
        value={
            "last_attempt_at_utc": started_at_utc,
            "last_completed_at_utc": None,
            "last_status": "running",
            "last_exit_code": None,
            "last_run_week_key": last_run_week_key,
            "running": True,
            "active_pid": int(proc.pid),
            "active_week_key": week_key,
            "active_log_path": str(run_log_path),
            "schedule_weekday": schedule_weekday,
            "schedule_hour": int(cfg.schedule_hour),
            "schedule_minute": int(cfg.schedule_minute),
            "tz_offset_minutes": int(cfg.tz_offset_minutes),
            "timeout_seconds": timeout_seconds,
        },
    )

    global _ACTIVE_PROC, _ACTIVE_WEEK_KEY, _ACTIVE_STARTED_AT_UTC, _ACTIVE_LOG_PATH
    with _ACTIVE_LOCK:
        _ACTIVE_PROC = proc
        _ACTIVE_WEEK_KEY = week_key
        _ACTIVE_STARTED_AT_UTC = started_at_utc
        _ACTIVE_LOG_PATH = run_log_path

    watcher = threading.Thread(
        target=_watch_active_run,
        kwargs={
            "proc": proc,
            "storage": storage,
            "cfg": cfg,
            "root_dir": root_dir,
            "log": log,
        },
        name="ml-retrain-watcher",
        daemon=True,
    )
    watcher.start()
    log("cycle", f"Weekly ML retrain started in background (pid={proc.pid}, week={week_key}).")
