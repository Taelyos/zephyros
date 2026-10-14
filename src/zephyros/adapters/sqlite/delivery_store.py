"""Deliveries, acceptance tests and sources in SQLite."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from zephyros.adapters.sqlite.database import Database
from zephyros.core.deliveries import DeliveryRecord, DeliveryState, Source, StaleRecordError

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    project_id  TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    body        TEXT NOT NULL,
    PRIMARY KEY (project_id, delivery_id)
);
CREATE TABLE IF NOT EXISTS acceptance_tests (
    project_id TEXT NOT NULL,
    path       TEXT NOT NULL,
    content    BLOB NOT NULL,
    PRIMARY KEY (project_id, path)
);
CREATE TABLE IF NOT EXISTS project_sources (
    project_id TEXT PRIMARY KEY,
    body       TEXT NOT NULL
);
"""


def _record_body(record: DeliveryRecord) -> str:
    return json.dumps(
        {
            "branch": record.branch,
            "state": record.state.value,
            "received_at": record.received_at,
            "changed_paths": list(record.changed_paths),
            "decision": record.decision,
            "outcomes": dict(record.outcomes),
            "report": record.report,
            "gate_report": record.gate_report,
            "contractor_gate_report": record.contractor_gate_report,
            "addendum": record.addendum,
            "pull_request": record.pull_request,
            "detail": record.detail,
            "pull_number": record.pull_number,
            "contract_version": record.contract_version,
            "inspections": {key: dict(value) for key, value in record.inspections.items()},
            "review": dict(record.review) if record.review is not None else None,
            "flagged_paths": list(record.flagged_paths),
            "revision": record.revision,
        },
        sort_keys=True,
    )


def _revision(body: dict[str, object]) -> int:
    """The revision of a stored record. One written before records had a revision was
    written once at least."""
    revision = body.get("revision", 1)
    return revision if isinstance(revision, int) else 1


def _record(row: sqlite3.Row) -> DeliveryRecord:
    body = json.loads(row["body"])
    return DeliveryRecord(
        project_id=row["project_id"],
        delivery_id=row["delivery_id"],
        branch=body["branch"],
        state=DeliveryState(body["state"]),
        received_at=body["received_at"],
        changed_paths=tuple(body["changed_paths"]),
        decision=body["decision"],
        outcomes=body["outcomes"],
        report=body.get("report"),
        # A record kept before had its report whole, the addendum inside: it reads as the report of
        # the gate, and a new inspection writes its addendum after it.
        gate_report=body["gate_report"] if "gate_report" in body else body.get("report"),
        addendum=body.get("addendum"),
        # A record kept before has no report for the Contractor: it gets none.
        contractor_gate_report=body.get("contractor_gate_report"),
        pull_request=body["pull_request"],
        detail=body["detail"],
        # Added by ADR 0020: a record written before has none of them.
        pull_number=body.get("pull_number"),
        contract_version=body.get("contract_version"),
        inspections=body.get("inspections") or {},
        review=body.get("review"),
        flagged_paths=tuple(body.get("flagged_paths") or ()),
        revision=_revision(body),
    )


class SqliteDeliveryStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def put_record(self, record: DeliveryRecord) -> DeliveryRecord:
        written = replace(record, revision=record.revision + 1)
        with self._db.transaction() as connection:
            # The read and the write are one transaction: no other write comes in between.
            row = connection.execute(
                "SELECT body FROM deliveries WHERE project_id = ? AND delivery_id = ?",
                (record.project_id, record.delivery_id),
            ).fetchone()
            stored = 0 if row is None else _revision(json.loads(row["body"]))
            if record.revision != stored:
                raise StaleRecordError(f"delivery {record.delivery_id} changed since it was read")
            connection.execute(
                "INSERT INTO deliveries (project_id, delivery_id, seq, body) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (project_id, delivery_id) DO UPDATE SET body = excluded.body",
                (
                    record.project_id,
                    record.delivery_id,
                    int(record.delivery_id[1:]),
                    _record_body(written),
                ),
            )
        return written

    def get_record(self, project_id: str, delivery_id: str) -> DeliveryRecord | None:
        rows = self._db.query(
            "SELECT * FROM deliveries WHERE project_id = ? AND delivery_id = ?",
            (project_id, delivery_id),
        )
        return _record(rows[0]) if rows else None

    def records(self, project_id: str) -> list[DeliveryRecord]:
        rows = self._db.query(
            "SELECT * FROM deliveries WHERE project_id = ? ORDER BY seq", (project_id,)
        )
        return [_record(row) for row in rows]

    def put_acceptance(self, project_id: str, path: str, content: bytes) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO acceptance_tests (project_id, path, content) VALUES (?, ?, ?) "
                "ON CONFLICT (project_id, path) DO UPDATE SET content = excluded.content",
                (project_id, path, content),
            )

    def acceptance(self, project_id: str) -> dict[str, bytes]:
        rows = self._db.query(
            "SELECT path, content FROM acceptance_tests WHERE project_id = ?", (project_id,)
        )
        return {row["path"]: bytes(row["content"]) for row in rows}

    def put_source(self, project_id: str, source: Source) -> None:
        body = json.dumps(
            {
                "repository": source.repository,
                "branch": source.branch,
                "state": source.state,
                "base": source.base,
                "detail": source.detail,
            },
            sort_keys=True,
        )
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO project_sources (project_id, body) VALUES (?, ?) "
                "ON CONFLICT (project_id) DO UPDATE SET body = excluded.body",
                (project_id, body),
            )

    def get_source(self, project_id: str) -> Source | None:
        rows = self._db.query(
            "SELECT body FROM project_sources WHERE project_id = ?", (project_id,)
        )
        if not rows:
            return None
        body = json.loads(rows[0]["body"])
        return Source(
            body["repository"], body["branch"], body["state"], body["base"], body["detail"]
        )
