"""SQLite persistence for workflow executions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from database import database_connection, database_path


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: str | None = None):
        self.path = database_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
                    input_json TEXT NOT NULL,
                    output TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connection(self):
        return database_connection(self.path)

    def start_run(self, run_id: str, workflow: str, input_data: dict[str, Any]) -> None:
        now = _timestamp()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO workflow_runs
                   (run_id, workflow, status, input_json, created_at, updated_at)
                   VALUES (?, ?, 'running', ?, ?, ?)""",
                (run_id, workflow, json.dumps(input_data, ensure_ascii=False), now, now),
            )

    def finish_run(self, run_id: str, output: str) -> None:
        self._update_run(run_id, "succeeded", output=output)

    def fail_run(self, run_id: str, error: str) -> None:
        self._update_run(run_id, "failed", error=error)

    def _update_run(
        self,
        run_id: str,
        status: str,
        output: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE workflow_runs
                   SET status = ?, output = ?, error = ?, updated_at = ?
                   WHERE run_id = ? AND status = 'running'""",
                (status, output, error, _timestamp(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No running workflow run found for {run_id}")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["input"] = json.loads(result.pop("input_json"))
        return result
