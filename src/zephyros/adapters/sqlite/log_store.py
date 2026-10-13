"""The audit log in SQLite: an insert-only table, a head kept in another table, and an anchor kept
in a file beside the database (ADR 0019).

Triggers refuse every update and delete of a record, so that the hub's own code cannot
rewrite the log by accident. They are not a defence against someone who owns the file:
detecting that is the job of the chain, of the head and of the anchor.

The anchor names, for each stream, the last record the hub stored. Before a record goes into
the database, the anchor names it as the next one; once it is stored, the anchor moves to it.
It moves only from the head it names: a store whose records or heads were replaced does not
take the anchor along, an anchor that is gone or unreadable is never written again, and a
stream taken out of it comes back only with a first record. A hub stopped between the two
writes leaves an anchor that names a next record: it counts when the store holds exactly that
record, and is dropped otherwise.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from zephyros.adapters.sqlite.database import Database
from zephyros.core.audit import (
    GENESIS_HASH,
    Action,
    AnchorMissingError,
    AnchorUnreadableError,
    AuditRecord,
    Head,
    LogError,
    Outcome,
    Reason,
)

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
_HASH = re.compile(r"[0-9a-f]{64}")

Entry = dict[str, object]
"""What the anchor keeps of a stream: `seq` and `hash` of its last stored record (0 and the
genesis hash before the first), and `next` while a record is on its way to the store."""


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


def _is_seq(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _check_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or not {"seq", "hash"} <= set(entry) <= {"seq", "hash", "next"}:
        return False
    if not (_is_seq(entry["seq"]) and _is_hash(entry["hash"])):
        return False
    if "next" not in entry:
        return True
    following = entry["next"]
    return (
        isinstance(following, dict)
        and set(following) == {"seq", "hash"}
        and _is_seq(following["seq"])
        and following["seq"] == entry["seq"] + 1
        and _is_hash(following["hash"])
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
                self._announce(record)
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
        except (sqlite3.Error, OSError) as error:
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
        self._settle(head)

    def anchor(self) -> Mapping[str, Head] | None:
        """The heads kept outside the database file (EXG-19, ATK-69), by stream: replacing the
        whole log, its head table included, cannot move them. None when this store keeps no
        anchor; AnchorMissingError or AnchorUnreadableError when it keeps one it cannot read."""
        if self._anchor is None:
            return None
        return {stream: self._head_of(stream, entry) for stream, entry in self._load().items()}

    def start_anchor(self) -> None:
        if self._anchor is None:
            raise LogError("this store keeps no anchor")
        if self._anchor.exists() or self.streams():
            raise LogError("an anchor starts only with an empty log")
        self._write({})

    def _announce(self, record: AuditRecord) -> None:
        """Name `record` in the anchor as the next record of its stream, when the anchor's head
        is the record it follows (or when it is the first record of a stream the anchor does
        not know). Otherwise the anchor stays as it is, for the verification to report."""
        kept = self._load_if_readable()
        if kept is None:
            return
        entry = kept.get(record.stream)
        if entry is None:
            if record.seq != 1:
                return
            head = Head(record.stream, 0, GENESIS_HASH)
        else:
            head = self._head_of(record.stream, entry)
        if (head.seq, head.hash) != (record.seq - 1, record.previous):
            return
        kept[record.stream] = {
            "seq": head.seq,
            "hash": head.hash,
            "next": {"seq": record.seq, "hash": record.hash},
        }
        self._write(kept)

    def _settle(self, head: Head) -> None:
        """The record the anchor named as next is stored: the anchor moves to it."""
        kept = self._load_if_readable()
        if kept is None:
            return
        entry = kept.get(head.stream)
        if entry is None or entry.get("next") != {"seq": head.seq, "hash": head.hash}:
            return
        kept[head.stream] = {"seq": head.seq, "hash": head.hash}
        self._write(kept)

    def _head_of(self, stream: str, entry: Entry) -> Head:
        """The head an entry stands for: its next record when the store holds exactly that
        record (a hub stopped before the anchor moved), its own head otherwise."""
        following = entry.get("next")
        if isinstance(following, dict):
            rows = self._db.query(
                "SELECT hash FROM audit_records WHERE stream = ? AND seq = ?",
                (stream, following["seq"]),
            )
            if rows and rows[0]["hash"] == following["hash"]:
                return Head(stream, cast("int", following["seq"]), cast("str", following["hash"]))
        return Head(stream, cast("int", entry["seq"]), cast("str", entry["hash"]))

    def _load(self) -> dict[str, Entry]:
        assert self._anchor is not None  # noqa: S101  # reason: the callers check it
        try:
            text = self._anchor.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise AnchorMissingError(str(self._anchor)) from error
        except OSError as error:
            raise AnchorUnreadableError(error.strerror or type(error).__name__) from error
        try:
            kept = json.loads(text)
        except ValueError as error:
            raise AnchorUnreadableError("it is not JSON") from error
        if not isinstance(kept, dict):
            raise AnchorUnreadableError("it is not a table of streams")
        if not all(_check_entry(entry) for entry in kept.values()):
            raise AnchorUnreadableError("an entry is not the head of a stream")
        return kept

    def _load_if_readable(self) -> dict[str, Entry] | None:
        """The anchor to update, or None when there is none to update: this store keeps no
        anchor, or the one it keeps is gone or unreadable — then it is left as it is."""
        if self._anchor is None:
            return None
        try:
            return self._load()
        except (AnchorMissingError, AnchorUnreadableError):
            return None

    def _write(self, kept: Mapping[str, Entry]) -> None:
        assert self._anchor is not None  # noqa: S101  # reason: the callers check it
        temporary = self._anchor.with_suffix(self._anchor.suffix + ".tmp")
        temporary.write_text(json.dumps(kept, sort_keys=True), encoding="utf-8")
        temporary.replace(self._anchor)

    def streams(self) -> list[str]:
        return [
            row["stream"]
            for row in self._db.query("SELECT DISTINCT stream FROM audit_records ORDER BY stream")
        ]
