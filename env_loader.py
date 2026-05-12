from __future__ import annotations

import os
from pathlib import Path


_ENV_FILENAMES = (".env.local", ".env")


def load_local_env(*, root_dir: Path | None = None, override: bool = False) -> None:
    """Load simple KEY=VALUE pairs from repo-local .env files into os.environ.

    Supported features:
    - .env.local then .env loading order
    - blank lines and comment lines starting with '#'
    - optional leading 'export '
    - single or double quoted values

    Existing process environment variables win unless override=True.
    """
    base_dir = Path(root_dir) if root_dir is not None else Path(__file__).resolve().parent
    for filename in _ENV_FILENAMES:
        env_path = base_dir / filename
        if not env_path.exists() or not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if override or key not in os.environ:
                os.environ[key] = value
