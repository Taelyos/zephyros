"""Instance records in SQLite: which instance has which subnet, so a restart finds them."""

from __future__ import annotations

import sqlite3

from zephyros.adapters.sqlite.database import Database
from zephyros.core.clock import format_time, parse_time
from zephyros.core.instances import InstanceRecord
from zephyros.core.ports.agent_engine import Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    project_id  TEXT NOT NULL,
    side        TEXT NOT NULL,
    instance_id TEXT NOT NULL UNIQUE,
    subnet      TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, side)
);
"""


def _record(row: sqlite3.Row) -> InstanceRecord:
    return InstanceRecord(
        project_id=row["project_id"],
        side=Side(row["side"]),
        instance_id=row["instance_id"],
        subnet=row["subnet"],
        created_at=parse_time(row["created_at"]),
    )


class SqliteInstanceStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def get(self, project_id: str, side: Side) -> InstanceRecord | None:
        rows = self._db.query(
            "SELECT * FROM instances WHERE project_id = ? AND side = ?", (project_id, side.value)
        )
        return _record(rows[0]) if rows else None

    def put(self, record: InstanceRecord) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO instances (project_id, side, instance_id, subnet, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (project_id, side) DO UPDATE SET "
                "instance_id = excluded.instance_id, subnet = excluded.subnet, "
                "created_at = excluded.created_at",
                (
                    record.project_id,
                    record.side.value,
                    record.instance_id,
                    record.subnet,
                    format_time(record.created_at),
                ),
            )

    def remove(self, project_id: str, side: Side) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM instances WHERE project_id = ? AND side = ?",
                (project_id, side.value),
            )

    def all(self) -> list[InstanceRecord]:
        rows = self._db.query("SELECT * FROM instances ORDER BY project_id, side")
        return [_record(row) for row in rows]
