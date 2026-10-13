"""The SQLite store of deliveries keeps what ADR 0020 adds to a delivery, and reads the records
written before. Both stores refuse a write made from a stale read (T05)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from zephyros.adapters.memory.delivery_store import InMemoryDeliveryStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.core.deliveries import DeliveryRecord, DeliveryState, StaleRecordError


def test_a_record_keeps_its_contract_version_inspections_review_and_flags() -> None:
    store = SqliteDeliveryStore(Database(":memory:"))
    record = DeliveryRecord(
        "p_store001",
        "d1",
        "work",
        DeliveryState.PROPOSED,
        "2026-10-13T08:00:00Z",
        ("CONTRACT.md", "app.py"),
        decision="CONCERNS",
        contract_version=3,
        inspections={
            "CTR-01": {"verdict": "conforms", "observation": "stdlib", "by": "o", "at": "t"}
        },
        review={"verdict": "accept", "reasons": "fine", "by": "o", "at": "t"},
        flagged_paths=("CONTRACT.md",),
        gate_report="# Evidence report\n",
        addendum="## Inspections recorded after the gate\n",
    )
    written = store.put_record(record)
    assert written == replace(record, revision=1)
    assert store.get_record("p_store001", "d1") == written
    assert written.report == (
        "# Evidence report\n\n## Inspections recorded after the gate\n"
    )  # T11: the report of the gate, then the addendum


def test_a_report_kept_whole_before_t11_reads_as_the_report_of_the_gate() -> None:
    """Before T11 a record kept its report with the addendum inside: it reads as it was."""
    database = Database(":memory:")
    store = SqliteDeliveryStore(database)
    whole = "# Evidence report\n\n## Inspections recorded after the gate\n\n| CTR-01 |\n"
    body: dict[str, object] = {
        "branch": "work",
        "state": "proposed",
        "received_at": "2026-10-13T08:00:00Z",
        "changed_paths": [],
        "decision": "PASS",
        "outcomes": {},
        "report": whole,
        "pull_request": None,
        "detail": None,
    }
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO deliveries (project_id, delivery_id, seq, body) VALUES (?, ?, ?, ?)",
            ("p_store001", "d1", 1, json.dumps(body)),
        )
    found = store.get_record("p_store001", "d1")
    assert found is not None
    assert (found.gate_report, found.addendum, found.report) == (whole, None, whole)


def test_a_record_written_before_adr_0020_reads_with_empty_additions() -> None:
    database = Database(":memory:")
    store = SqliteDeliveryStore(database)
    body: dict[str, object] = {
        "branch": "work",
        "state": "decided",
        "received_at": "2026-10-12T08:00:00Z",
        "changed_paths": ["app.py"],
        "decision": "PASS",
        "outcomes": {},
        "report": None,
        "pull_request": None,
        "detail": None,
    }
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO deliveries (project_id, delivery_id, seq, body) VALUES (?, ?, ?, ?)",
            ("p_store001", "d1", 1, json.dumps(body)),
        )
    found = store.get_record("p_store001", "d1")
    assert found is not None
    assert (found.contract_version, found.inspections, found.review, found.flagged_paths) == (
        None,
        {},
        None,
        (),
    )
    # T05: it was written once at least. A write from this read goes through; a new record of
    # the same number, at revision 0, does not.
    assert found.revision == 1
    with pytest.raises(StaleRecordError):
        store.put_record(replace(found, revision=0))
    assert store.put_record(replace(found, detail="read again")).revision == 2


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> InMemoryDeliveryStore | SqliteDeliveryStore:
    if request.param == "memory":
        return InMemoryDeliveryStore()
    return SqliteDeliveryStore(Database(":memory:"))


def test_a_write_made_from_a_stale_read_is_refused_and_changes_nothing(
    store: InMemoryDeliveryStore | SqliteDeliveryStore,
) -> None:
    """T05: two writers read revision 1; the first write wins, the second is refused."""
    first = store.put_record(
        DeliveryRecord("p_store001", "d1", "work", DeliveryState.DECIDED, "2026-10-14T08:00:00Z")
    )
    inspected = store.put_record(replace(first, decision="PASS"))
    with pytest.raises(StaleRecordError):
        store.put_record(replace(first, detail="no pull request"))
    assert store.get_record("p_store001", "d1") == inspected
    assert inspected.revision == 2
    # a new record is written once, never over another
    with pytest.raises(StaleRecordError):
        store.put_record(
            DeliveryRecord("p_store001", "d1", "again", DeliveryState.RECEIVED, "2026-10-14T09:00Z")
        )
