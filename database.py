"""Shared SQLite connection helpers for the jrocai core database."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def database_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get("JROCAI_DB_PATH", "jroc_core.db"))


@contextmanager
def database_connection(path: str | os.PathLike[str] | None = None) -> Iterator[sqlite3.Connection]:
    db_path = database_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()
