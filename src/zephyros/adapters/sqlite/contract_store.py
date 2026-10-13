"""The contract in SQLite: immutable versions, and changes that wait for a decision."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from zephyros.adapters.sqlite.database import Database
from zephyros.core.audit import canonical_json
from zephyros.core.clock import format_time, parse_time
from zephyros.core.contract import (
    ChangeStatus,
    ContractVersion,
    PendingChange,
    operations_from_data,
    operations_to_data,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_versions (
    project_id       TEXT    NOT NULL,
    number           INTEGER NOT NULL,
    version_id       TEXT    NOT NULL,
    parent           TEXT,
    content_hash     TEXT    NOT NULL,
    objects          TEXT    NOT NULL,
    author           TEXT    NOT NULL,
    time             TEXT    NOT NULL,
    operator_written INTEGER NOT NULL,
    PRIMARY KEY (project_id, number),
    UNIQUE (project_id, version_id)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS contract_versions_no_update BEFORE UPDATE ON contract_versions
BEGIN SELECT RAISE(ABORT, 'contract versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS contract_versions_no_delete BEFORE DELETE ON contract_versions
BEGIN SELECT RAISE(ABORT, 'contract versions are immutable'); END;
CREATE TABLE IF NOT EXISTS contract_changes (
    project_id        TEXT NOT NULL,
    change_id         TEXT NOT NULL,
    proposer          TEXT NOT NULL,
    base_version      INTEGER NOT NULL,
    summary           TEXT NOT NULL,
    operations        TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    resolved_by       TEXT,
    resolved_at       TEXT,
    resulting_version TEXT,
    PRIMARY KEY (project_id, change_id)
);
"""

_VERSION = "number, version_id, parent, content_hash, objects, author, time, operator_written"
_CHANGE = (
    "project_id, change_id, proposer, base_version, summary, operations, status, created_at, "
    "resolved_by, resolved_at, resulting_version"
)


def _version(row: sqlite3.Row) -> ContractVersion:
    return ContractVersion(
        number=row["number"],
        id=row["version_id"],
        parent=row["parent"],
        content_hash=row["content_hash"],
        objects=json.loads(row["objects"]),
        author=row["author"],
        time=parse_time(row["time"]),
        operator_written=bool(row["operator_written"]),
    )


def _change(row: sqlite3.Row) -> PendingChange:
    return PendingChange(
        id=row["change_id"],
        project_id=row["project_id"],
        proposer=row["proposer"],
        base_version=row["base_version"],
        summary=row["summary"],
        operations=operations_from_data(json.loads(row["operations"])),
        status=ChangeStatus(row["status"]),
        created_at=parse_time(row["created_at"]),
        resolved_by=row["resolved_by"],
        resolved_at=None if row["resolved_at"] is None else parse_time(row["resolved_at"]),
        resulting_version=row["resulting_version"],
    )


def _insert_version(
    connection: sqlite3.Connection, project_id: str, version: ContractVersion
) -> None:
    last = connection.execute(
        "SELECT MAX(number) FROM contract_versions WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    expected = 1 if last is None else last + 1
    if version.number != expected:
        raise ValueError(f"expected version {expected}, got {version.number}")
    connection.execute(
        "INSERT INTO contract_versions (project_id, number, version_id, parent, content_hash, "
        "objects, author, time, operator_written) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            version.number,
            version.id,
            version.parent,
            version.content_hash,
            canonical_json({key: dict(item) for key, item in version.objects.items()}),
            version.author,
            format_time(version.time),
            int(version.operator_written),
        ),
    )


class SqliteContractStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def latest(self, project_id: str) -> ContractVersion | None:
        rows = self._db.query(
            f"SELECT {_VERSION} FROM contract_versions WHERE project_id = ? "
            "ORDER BY number DESC LIMIT 1",
            (project_id,),
        )
        return _version(rows[0]) if rows else None

    def get(self, project_id: str, number: int) -> ContractVersion | None:
        rows = self._db.query(
            f"SELECT {_VERSION} FROM contract_versions WHERE project_id = ? AND number = ?",
            (project_id, number),
        )
        return _version(rows[0]) if rows else None

    def numbers(self, project_id: str) -> list[int]:
        rows = self._db.query(
            "SELECT number FROM contract_versions WHERE project_id = ? ORDER BY number",
            (project_id,),
        )
        return [row["number"] for row in rows]

    def append(self, project_id: str, version: ContractVersion) -> None:
        with self._db.transaction() as connection:
            _insert_version(connection, project_id, version)

    def add_pending(self, change: PendingChange) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                f"INSERT INTO contract_changes ({_CHANGE}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    change.project_id,
                    change.id,
                    change.proposer,
                    change.base_version,
                    change.summary,
                    canonical_json(operations_to_data(change.operations)),
                    change.status.value,
                    format_time(change.created_at),
                    change.resolved_by,
                    None if change.resolved_at is None else format_time(change.resolved_at),
                    change.resulting_version,
                ),
            )

    def get_pending(self, project_id: str, change_id: str) -> PendingChange | None:
        rows = self._db.query(
            f"SELECT {_CHANGE} FROM contract_changes WHERE project_id = ? AND change_id = ?",
            (project_id, change_id),
        )
        return _change(rows[0]) if rows else None

    def pending(self, project_id: str) -> list[PendingChange]:
        rows = self._db.query(
            f"SELECT {_CHANGE} FROM contract_changes WHERE project_id = ? AND status = 'pending' "
            "ORDER BY created_at, change_id",
            (project_id,),
        )
        return [_change(row) for row in rows]

    def _resolve(
        self,
        project_id: str,
        change_id: str,
        status: ChangeStatus,
        by: str,
        at: datetime,
        version: ContractVersion | None,
    ) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE contract_changes SET status = ?, resolved_by = ?, resolved_at = ?, "
                "resulting_version = ? WHERE project_id = ? AND change_id = ? "
                "AND status = 'pending'",
                (
                    status.value,
                    by,
                    format_time(at),
                    None if version is None else version.id,
                    project_id,
                    change_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if version is not None:
                _insert_version(connection, project_id, version)
            return True

    def reject(self, project_id: str, change_id: str, by: str, at: datetime) -> bool:
        return self._resolve(project_id, change_id, ChangeStatus.REJECTED, by, at, None)

    def approve(
        self, project_id: str, change_id: str, by: str, at: datetime, version: ContractVersion
    ) -> bool:
        return self._resolve(project_id, change_id, ChangeStatus.APPROVED, by, at, version)
