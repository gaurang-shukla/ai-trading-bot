"""Project-local configuration loading."""

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def load_project_env(path: Path | None = None) -> Path:
    """Load a dotenv file without making configuration depend on the current directory.

    Values already supplied by the process environment take precedence over the local
    file.  This intentionally small parser supports the syntax used by ``.env.example``.
    """
    env_file = path or ENV_FILE
    if not env_file.is_file():
        return env_file

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    return env_file

