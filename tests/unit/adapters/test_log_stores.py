"""Both log stores behave the same; the SQLite one also refuses to rewrite a record."""

from __future__ import annotations

import dataclasses
import json
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


# ----------------------------------------------------------------------------- the anchor
# ADR 0019, ATK-69: the anchor is the hub's own memory of the heads of its log, kept outside the
# database. It starts with the log; afterwards nothing starts it again, and it moves only from
# the head it names. An anchor that is gone, unreadable, or silent about a stream fails the
# verification: it never reads as "no anchor".
MISSING = "the anchor kept outside the store is missing"
UNREADABLE = "the anchor kept outside the store cannot be read"
SILENT = "the anchor kept outside the store does not name this stream"
DIFFERENT = "the log does not match the anchor kept outside the store"


def anchored_log(tmp_path: Path) -> tuple[Path, Path]:
    """A log in a file, its anchor beside it, three records in each of two streams."""
    path, anchor = tmp_path / "hub.db", tmp_path / "log-anchor.json"
    database = Database(path)
    log = AuditLog(SqliteLogStore(database, anchor), FakeClock())
    for number in range(3):
        log.append(event("p_a", number))
        log.append(event("p_b", number))
    database.close()
    return path, anchor


def reopen(path: Path, anchor: Path) -> tuple[Database, AuditLog]:
    """The hub starts again on the same files."""
    database = Database(path)
    return database, AuditLog(SqliteLogStore(database, anchor), FakeClock())


def anchor_records(log: AuditLog) -> list[tuple[str, dict[str, object]]]:
    return [
        (record.outcome.value, json.loads(record.parameters))
        for record in log.read("hub")
        if record.action is Action.LOG_ANCHOR
    ]


def test_the_anchor_starts_with_the_log_and_its_start_is_logged(tmp_path: Path) -> None:
    path, anchor = tmp_path / "hub.db", tmp_path / "log-anchor.json"
    database = Database(path)
    log = AuditLog(SqliteLogStore(database, anchor), FakeClock())
    assert anchor.exists()
    assert anchor_records(log) == [("ok", {"anchor": "started"})]
    log.append(event())
    assert log.verify("p_a").ok
    assert log.verify("hub").ok
    database.close()
    database, log = reopen(path, anchor)  # a restart starts nothing
    assert anchor_records(log) == [("ok", {"anchor": "started"})]
    assert log.verify("p_a").ok
    database.close()


def test_a_deleted_anchor_fails_the_verification_and_is_never_started_again(
    tmp_path: Path,
) -> None:
    path, anchor = anchored_log(tmp_path)
    anchor.unlink()  # the file beside hub.db, deleted by someone who reaches the data
    database, log = reopen(path, anchor)
    for stream in ("p_a", "p_b"):
        report = log.verify(stream)
        assert (report.ok, report.first_bad_seq, report.problem) == (False, None, MISSING)
    assert anchor_records(log) == [("ok", {"anchor": "started"}), ("error", {"anchor": "missing"})]
    log.append(event("p_a", 9))  # the hub goes on: no new anchor follows the store,
    log.append(event("p_c"))  # not even with the first record of a new stream
    assert not anchor.exists()
    assert log.verify("p_a").problem == MISSING
    assert log.verify("p_c").problem == MISSING
    database.close()


A, B = "a" * 64, "b" * 64
GARBAGE = [
    "not json",
    "[]",
    json.dumps({"p_a": 3}),
    json.dumps({"p_a": {"seq": "3", "hash": A}}),
    json.dumps({"p_a": {"seq": True, "hash": A}}),
    json.dumps({"p_a": {"seq": 3, "hash": "short"}}),
    json.dumps({"p_a": {"seq": 3, "hash": A, "extra": 1}}),
    json.dumps({"p_a": {"seq": 3, "hash": A, "next": {"seq": 5, "hash": B}}}),
    json.dumps({"p_a": {"seq": 0, "hash": A, "next": {"seq": True, "hash": B}}}),
]


@pytest.mark.parametrize("garbage", GARBAGE)
def test_an_unreadable_anchor_fails_the_verification_and_is_left_as_found(
    tmp_path: Path, garbage: str
) -> None:
    path, anchor = anchored_log(tmp_path)
    anchor.write_text(garbage, encoding="utf-8")
    database, log = reopen(path, anchor)
    for stream in ("p_a", "p_b"):
        report = log.verify(stream)
        assert not report.ok
        assert report.first_bad_seq is None
        assert (report.problem or "").startswith(UNREADABLE + ": "), report.problem
    assert [outcome for outcome, _ in anchor_records(log)] == ["ok", "error"]
    assert anchor_records(log)[-1][1]["anchor"] == "unreadable"
    log.append(event("p_a", 9))
    log.append(event("p_c"))
    assert anchor.read_text(encoding="utf-8") == garbage  # kept for whoever looks into it
    database.close()


def test_a_stream_taken_out_of_the_anchor_fails_its_verification(tmp_path: Path) -> None:
    path, anchor = anchored_log(tmp_path)
    kept = json.loads(anchor.read_text(encoding="utf-8"))
    del kept["p_a"]
    anchor.write_text(json.dumps(kept), encoding="utf-8")
    database, log = reopen(path, anchor)
    report = log.verify("p_a")
    assert (report.ok, report.first_bad_seq, report.problem) == (False, None, SILENT)
    assert log.verify("p_b").ok
    log.append(event("p_a", 9))  # its next record does not put the stream back
    log.append(event("p_b", 9))
    assert log.verify("p_a").problem == SILENT
    assert log.verify("p_b").ok
    database.close()


def test_a_replaced_log_stays_detected_after_the_hub_appends_to_it(tmp_path: Path) -> None:
    """ATK-69: the store is replaced by a consistent forgery while the hub is stopped. When the
    hub starts again and logs its next call, the anchor must not follow the forged head."""
    path, anchor = anchored_log(tmp_path)
    forged_store = SqliteLogStore(Database(":memory:"))
    AuditLog(forged_store, FakeClock()).append(event(number=7))
    record = forged_store.last("p_a")
    assert record is not None
    attacker = sqlite3.connect(str(path))
    attacker.execute("DROP TRIGGER audit_records_no_delete")
    attacker.execute("DELETE FROM audit_records WHERE stream = 'p_a'")
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
        "UPDATE audit_heads SET seq = ?, hash = ? WHERE stream = 'p_a'", (1, record.hash)
    )
    attacker.commit()
    attacker.close()
    database, log = reopen(path, anchor)
    assert log.verify("p_a").problem == DIFFERENT
    log.append(event("p_a", 9))  # the hub's next call, on top of the forgery
    report = log.verify("p_a")
    assert (report.ok, report.problem) == (False, DIFFERENT)
    assert log.verify("p_b").ok
    database.close()


def test_a_stop_before_the_anchor_moved_is_not_taken_for_a_tampering(tmp_path: Path) -> None:
    """The anchor names a record before the store holds it, and moves to it once it is stored:
    a hub stopped between the two writes leaves a log that verifies and goes on."""
    path, anchor = anchored_log(tmp_path)
    database = Database(path)
    store = SqliteLogStore(database, anchor)
    log = AuditLog(store, FakeClock())
    seen: list[str] = []
    original = store.set_head

    def set_head_then_stop(head: Head) -> None:
        seen.append(anchor.read_text(encoding="utf-8"))
        original(head)

    store.set_head = set_head_then_stop  # type: ignore[method-assign]  # reason: the test stops the hub between the two writes
    log.append(event("p_a", 9))
    anchor.write_text(seen[0], encoding="utf-8")  # as the stopped hub left it
    database.close()
    database, log = reopen(path, anchor)
    assert log.verify("p_a").ok
    log.append(event("p_a", 10))
    assert log.verify("p_a").ok
    assert json.loads(anchor.read_text(encoding="utf-8"))["p_a"]["seq"] == 5
    database.close()


def test_a_record_the_store_refused_leaves_the_anchor_where_it_was(tmp_path: Path) -> None:
    path, anchor = anchored_log(tmp_path)
    database, log = reopen(path, anchor)
    store = SqliteLogStore(database, anchor)
    last = store.last("p_a")
    assert last is not None
    before = anchor.read_text(encoding="utf-8")
    with pytest.raises(LogError):
        store.append(last)  # out of sequence: refused before anything is written
    assert anchor.read_text(encoding="utf-8") == before
    broken = dataclasses.replace(
        last,
        seq=last.seq + 1,
        previous=last.hash,
        parameters=None,  # type: ignore[arg-type]  # reason: a record the database refuses
    )
    with pytest.raises(LogError):
        store.append(broken)  # the database refuses it once the anchor names it
    assert log.verify("p_a").ok
    log.append(event("p_a", 9))
    assert log.verify("p_a").ok
    database.close()
