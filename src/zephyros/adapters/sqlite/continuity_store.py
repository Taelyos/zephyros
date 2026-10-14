"""The tasks of the continuity in SQLite: where each ladder stands, so a restart finds it (T12)."""

from __future__ import annotations

import sqlite3

from zephyros.adapters.sqlite.database import Database
from zephyros.core.clock import format_time, parse_time
from zephyros.core.continuity import Expectation, TaskState

SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_tasks (
    project_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    side       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    since      TEXT NOT NULL,
    last       TEXT NOT NULL,
    step       INTEGER NOT NULL,
    held       TEXT,
    PRIMARY KEY (project_id, kind)
);
"""


def _expectation(row: sqlite3.Row) -> Expectation:
    return Expectation(
        TaskState(
            project_id=row["project_id"],
            side=row["side"],
            kind=row["kind"],
            detail=row["detail"],
            since=parse_time(row["since"]),
        ),
        last=parse_time(row["last"]),
        step=int(row["step"]),
        held=row["held"],
    )


class SqliteContinuityStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def put(self, expectation: Expectation) -> None:
        state = expectation.state
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO continuity_tasks "
                "(project_id, kind, side, detail, since, last, step, held) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (project_id, kind) DO UPDATE SET "
                "side = excluded.side, detail = excluded.detail, since = excluded.since, "
                "last = excluded.last, step = excluded.step, held = excluded.held",
                (
                    state.project_id,
                    state.kind,
                    state.side,
                    state.detail,
                    format_time(state.since),
                    format_time(expectation.last),
                    expectation.step,
                    expectation.held,
                ),
            )

    def remove(self, project_id: str, kind: str) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM continuity_tasks WHERE project_id = ? AND kind = ?",
                (project_id, kind),
            )

    def all(self) -> list[Expectation]:
        rows = self._db.query("SELECT * FROM continuity_tasks ORDER BY project_id, kind")
        return [_expectation(row) for row in rows]
