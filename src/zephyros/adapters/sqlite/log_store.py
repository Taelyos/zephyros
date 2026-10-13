"""The audit log in SQLite: an insert-only table, and a head kept in another table.

Triggers refuse every update and delete of a record, so that the hub's own code cannot
rewrite the log by accident. They are not a defence against someone who owns the file:
detecting that is the job of the chain and of the external anchor of a later milestone.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zephyros.adapters.sqlite.database import Database
from zephyros.core.audit import Action, AuditRecord, Head, LogError, Outcome, Reason

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    stream     TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    time       TEXT    NOT NULL,
    principal  TEXT    NOT NULL,
    action     TEXT    NOT NULL,
    outcome    TEXT    NOT NULL,
    reason     TEXT    NOT NULL,
    parameters TEXT    NOT NULL,
    previous   TEXT    NOT NULL,
    hash       TEXT    NOT NULL,
    PRIMARY KEY (stream, seq)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS audit_records_no_update BEFORE UPDATE ON audit_records
BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_records_no_delete BEFORE DELETE ON audit_records
BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END;
CREATE TABLE IF NOT EXISTS audit_heads (
    stream TEXT PRIMARY KEY,
    seq    INTEGER NOT NULL,
    hash   TEXT    NOT NULL
);
"""

_COLUMNS = "stream, seq, time, principal, action, outcome, reason, parameters, previous, hash"


def _record(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(
        seq=row["seq"],
        time=row["time"],
        stream=row["stream"],
        principal=row["principal"],
        action=Action(row["action"]),
        outcome=Outcome(row["outcome"]),
        reason=Reason(row["reason"]),
        parameters=row["parameters"],
        previous=row["previous"],
        hash=row["hash"],
    )


class SqliteLogStore:
    def __init__(self, database: Database, anchor_path: Path | None = None) -> None:
        self._db = database
        self._anchor = anchor_path
        database.script(SCHEMA)

    def append(self, record: AuditRecord) -> None:
        try:
            with self._db.transaction() as connection:
                last = connection.execute(
                    "SELECT MAX(seq) FROM audit_records WHERE stream = ?", (record.stream,)
                ).fetchone()[0]
                expected = 1 if last is None else last + 1
                if record.seq != expected:
                    raise LogError(f"expected sequence number {expected}, got {record.seq}")
                connection.execute(
                    f"INSERT INTO audit_records ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.stream,
                        record.seq,
                        record.time,
                        record.principal,
                        record.action.value,
                        record.outcome.value,
                        record.reason.value,
                        record.parameters,
                        record.previous,
                        record.hash,
                    ),
                )
        except sqlite3.Error as error:
            raise LogError(f"the store refused the record: {error}") from error

    def last(self, stream: str) -> AuditRecord | None:
        rows = self._db.query(
            f"SELECT {_COLUMNS} FROM audit_records WHERE stream = ? ORDER BY seq DESC LIMIT 1",
            (stream,),
        )
        return _record(rows[0]) if rows else None

    def read(self, stream: str, after: int = 0, limit: int = 1000) -> list[AuditRecord]:
        rows = self._db.query(
            f"SELECT {_COLUMNS} FROM audit_records WHERE stream = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (stream, after, limit),
        )
        return [_record(row) for row in rows]

    def head(self, stream: str) -> Head | None:
        rows = self._db.query(
            "SELECT stream, seq, hash FROM audit_heads WHERE stream = ?", (stream,)
        )
        return Head(rows[0]["stream"], rows[0]["seq"], rows[0]["hash"]) if rows else None

    def set_head(self, head: Head) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_heads (stream, seq, hash) VALUES (?, ?, ?) "
                "ON CONFLICT(stream) DO UPDATE SET seq = excluded.seq, hash = excluded.hash",
                (head.stream, head.seq, head.hash),
            )
        self._write_anchor(head)

    def anchor(self, stream: str) -> Head | None:
        """The head kept outside the database file (EXG-19, ATK-69): replacing the whole log,
        its head table included, cannot move it."""
        if self._anchor is None or not self._anchor.exists():
            return None
        try:
            kept = json.loads(self._anchor.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        entry = kept.get(stream)
        if not isinstance(entry, dict):
            return None
        return Head(stream, int(entry.get("seq", 0)), str(entry.get("hash", "")))

    def _write_anchor(self, head: Head) -> None:
        if self._anchor is None:
            return
        kept: dict[str, object] = {}
        if self._anchor.exists():
            try:
                kept = json.loads(self._anchor.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                kept = {}
        kept[head.stream] = {"seq": head.seq, "hash": head.hash}
        temporary = self._anchor.with_suffix(self._anchor.suffix + ".tmp")
        temporary.write_text(json.dumps(kept, sort_keys=True), encoding="utf-8")
        temporary.replace(self._anchor)

    def streams(self) -> list[str]:
        return [
            row["stream"]
            for row in self._db.query("SELECT DISTINCT stream FROM audit_records ORDER BY stream")
        ]
