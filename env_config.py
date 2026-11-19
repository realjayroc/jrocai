"""Small dependency-free loader for project-local .env files."""

from __future__ import annotations

import os
import re
from pathlib import Path


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_local_env(path: str | os.PathLike[str] | None = None) -> bool:
    env_path = Path(path) if path is not None else Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return False
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(f"cannot read local environment file {env_path}: {error}") from error
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not _NAME.fullmatch(name):
            raise ValueError(f"invalid .env assignment on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "\n" in value or "\r" in value:
            raise ValueError(f"invalid .env value on line {line_number}")
        os.environ.setdefault(name, value)
    return True
