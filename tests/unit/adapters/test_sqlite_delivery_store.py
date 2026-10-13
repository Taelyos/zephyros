"""The SQLite store of deliveries keeps what ADR 0020 adds to a delivery, and reads the records
written before."""

from __future__ import annotations

import json

from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.core.deliveries import DeliveryRecord, DeliveryState


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
    )
    store.put_record(record)
    assert store.get_record("p_store001", "d1") == record


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
