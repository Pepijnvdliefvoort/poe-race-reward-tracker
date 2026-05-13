from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "name": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event", "session", "pid"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _build_logger(name: str, filename: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / filename

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Avoid duplicate handlers if reloaded.
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(path) for h in logger.handlers):
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(JsonlFormatter())
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)

    return logger


class _StreamToLogger:
    def __init__(self, logger: logging.Logger, level: int, fallback: TextIO) -> None:
        self._logger = logger
        self._level = level
        self._fallback = fallback
        self._buf = ""

    def write(self, data: str) -> int:
        if not data:
            return 0

        # Always keep original behavior (still prints to console).
        try:
            self._fallback.write(data)
            self._fallback.flush()
        except Exception:
            pass

        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line.strip():
                continue
            # If code prints a structured prefix like "[warn]" or "[error]" (common in this repo),
            # capture it at the correct severity so the admin log filters/counts work.
            lowered = line.lstrip().lower()
            if lowered.startswith("[warn]") or lowered.startswith("[warning]"):
                self._logger.warning(line)
                continue
            if lowered.startswith("[error]"):
                self._logger.error(line)
                continue
            if lowered.startswith("[critical]"):
                self._logger.critical(line)
                continue
            # Upgrade common "Warning:" prefixes to WARNING even if printed to stdout.
            if self._level == logging.INFO and lowered.startswith("warning:"):
                self._logger.warning(line)
            else:
                self._logger.log(self._level, line)
        return len(data)

    def flush(self) -> None:
        try:
            self._fallback.flush()
        except Exception:
            pass


def install_structured_logging(app_name: str, filename: str) -> logging.Logger:
    """
    Install a JSONL logger writing to logs/<filename> and capture print()/stderr as log records.

    - stdout -> INFO (or WARNING when line starts with "Warning:")
    - stderr -> ERROR
    """
    logger = _build_logger(app_name, filename)
    session = f"{int(datetime.now(timezone.utc).timestamp())}-{os.getpid()}-{secrets.token_hex(4)}"
    logger.info("session start", extra={"event": "session_start", "session": session, "pid": os.getpid()})
    sys.stdout = _StreamToLogger(logger, logging.INFO, sys.__stdout__)  # type: ignore[assignment]
    sys.stderr = _StreamToLogger(logger, logging.ERROR, sys.__stderr__)  # type: ignore[assignment]
    return logger

