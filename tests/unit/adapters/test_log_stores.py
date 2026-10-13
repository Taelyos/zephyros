"""Both log stores behave the same; the SQLite one also refuses to rewrite a record."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.log_store import SqliteLogStore
from zephyros.core.audit import (
    Action,
    AuditEvent,
    AuditLog,
    Head,
    LogError,
    LogStore,
    Outcome,
    Reason,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[LogStore]:
    if request.param == "memory":
        yield InMemoryLogStore()
    else:
        database = Database(":memory:")
        yield SqliteLogStore(database)
        database.close()


def event(stream: str = "p_a", number: int = 0) -> AuditEvent:
    return AuditEvent(
        stream, "operator", Action.CONTRACT_READ, Outcome.OK, parameters={"n": number}
    )


def test_append_read_last_head_and_streams(store: LogStore) -> None:
    log = AuditLog(store, FakeClock())
    for number in range(5):
        log.append(event(number=number))
    log.append(event("p_b"))
    assert [record.seq for record in store.read("p_a")] == [1, 2, 3, 4, 5]
    assert [record.seq for record in store.read("p_a", after=2, limit=2)] == [3, 4]
    last = store.last("p_a")
    assert last is not None
    assert (last.seq, store.head("p_a")) == (5, Head("p_a", 5, last.hash))
    assert store.last("p_none") is None
    assert store.head("p_none") is None
    assert store.read("p_none") == []
    assert store.streams() == ["p_a", "p_b"]
    assert log.verify("p_a").ok


def test_a_record_out_of_sequence_is_refused(store: LogStore) -> None:
    log = AuditLog(store, FakeClock())
    first = log.append(event())
    with pytest.raises(LogError, match="expected sequence number 2"):
        store.append(first)


def test_the_refusal_reasons_survive_storage(store: LogStore) -> None:
    log = AuditLog(store, FakeClock())
    log.append(
        AuditEvent(
            "p_a", "contractor:p_a", Action.CONTRACT_APPROVE, Outcome.REFUSED, Reason.SELF_APPROVAL
        )
    )
    record = store.read("p_a")[0]
    assert (record.action, record.outcome, record.reason) == (
        Action.CONTRACT_APPROVE,
        Outcome.REFUSED,
        Reason.SELF_APPROVAL,
    )


def test_sqlite_refuses_to_update_or_delete_a_record() -> None:
    database = Database(":memory:")
    log = AuditLog(SqliteLogStore(database), FakeClock())
    log.append(event())
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        database.transaction() as connection,
    ):
        connection.execute("UPDATE audit_records SET principal = 'someone'")
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        database.transaction() as connection,
    ):
        connection.execute("DELETE FROM audit_records")
    assert log.verify("p_a").ok


def test_sqlite_log_survives_a_restart_and_catches_an_edit_made_with_the_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hub.db"
    database = Database(path)
    log = AuditLog(SqliteLogStore(database), FakeClock())
    for number in range(4):
        log.append(event(number=number))
    database.close()

    reopened = Database(path)
    log = AuditLog(SqliteLogStore(reopened), FakeClock())
    assert log.verify("p_a").ok
    assert log.append(event(number=9)).seq == 5
    reopened.close()

    # An attacker who owns the file drops the trigger, then edits one record.
    attacker = sqlite3.connect(str(path))
    attacker.execute("DROP TRIGGER audit_records_no_update")
    attacker.execute("UPDATE audit_records SET principal = 'operator2' WHERE seq = 3")
    attacker.commit()
    attacker.close()
    checked = Database(path)
    report = AuditLog(SqliteLogStore(checked), FakeClock()).verify("p_a")
    assert (report.ok, report.first_bad_seq) == (False, 3)
    checked.close()


def test_sqlite_detects_a_truncated_tail_through_the_head(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    database = Database(path)
    log = AuditLog(SqliteLogStore(database), FakeClock())
    for number in range(4):
        log.append(event(number=number))
    database.close()
    attacker = sqlite3.connect(str(path))
    attacker.execute("DROP TRIGGER audit_records_no_delete")
    attacker.execute("DELETE FROM audit_records WHERE seq > 2")
    attacker.commit()
    attacker.close()
    checked = Database(path)
    report = AuditLog(SqliteLogStore(checked), FakeClock()).verify("p_a")
    assert not report.ok
    assert report.problem == "the last record does not match the head"
    checked.close()


def test_a_failed_transaction_leaves_nothing_behind() -> None:
    database = Database(":memory:")
    store = SqliteLogStore(database)
    log = AuditLog(store, FakeClock())
    log.append(event())
    with pytest.raises(RuntimeError), database.transaction():
        raise RuntimeError("boom")
    assert len(store.read("p_a")) == 1


def test_sqlite_detects_a_whole_log_replacement_through_the_anchor(tmp_path: Path) -> None:
    """ATK-69: the log, its head table included, is replaced by a consistently re-chained
    copy. The head table agrees with the forged records; only the anchor kept outside the
    file still names the records that were there."""
    path = tmp_path / "hub.db"
    anchor = tmp_path / "log-anchor.json"
    database = Database(path)
    log = AuditLog(SqliteLogStore(database, anchor), FakeClock())
    for number in range(4):
        log.append(event(number=number))
    database.close()

    # The forged copy: one honest record, built with the hub's own code, so the chain and the
    # head of the replacement are perfectly consistent.
    forged_store = SqliteLogStore(Database(":memory:"))
    AuditLog(forged_store, FakeClock()).append(event(number=7))
    record = forged_store.last("p_a")
    head = forged_store.head("p_a")
    assert record is not None
    assert head is not None

    attacker = sqlite3.connect(str(path))
    attacker.execute("DROP TRIGGER audit_records_no_update")
    attacker.execute("DROP TRIGGER audit_records_no_delete")
    attacker.execute("DELETE FROM audit_records")
    attacker.execute("DELETE FROM audit_heads")
    attacker.execute(
        "INSERT INTO audit_records (stream, seq, time, principal, action, outcome, reason, "
        "parameters, previous, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    attacker.execute(
        "INSERT INTO audit_heads (stream, seq, hash) VALUES (?, ?, ?)",
        (head.stream, head.seq, head.hash),
    )
    attacker.commit()
    attacker.close()

    checked = Database(path)
    report = AuditLog(SqliteLogStore(checked, anchor), FakeClock()).verify("p_a")
    assert not report.ok
    assert report.problem == "the log does not match the anchor kept outside the store"
    checked.close()
