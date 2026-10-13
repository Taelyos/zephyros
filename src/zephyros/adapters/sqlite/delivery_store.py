"""Deliveries, acceptance tests and sources in SQLite."""

from __future__ import annotations

import json
import sqlite3

from zephyros.adapters.sqlite.database import Database
from zephyros.core.deliveries import DeliveryRecord, DeliveryState, Source

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
            "pull_request": record.pull_request,
            "detail": record.detail,
            "pull_number": record.pull_number,
            "contract_version": record.contract_version,
            "inspections": {key: dict(value) for key, value in record.inspections.items()},
            "review": dict(record.review) if record.review is not None else None,
            "flagged_paths": list(record.flagged_paths),
        },
        sort_keys=True,
    )


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
        report=body["report"],
        pull_request=body["pull_request"],
        detail=body["detail"],
        # Added by ADR 0020: a record written before has none of them.
        pull_number=body.get("pull_number"),
        contract_version=body.get("contract_version"),
        inspections=body.get("inspections") or {},
        review=body.get("review"),
        flagged_paths=tuple(body.get("flagged_paths") or ()),
    )


class SqliteDeliveryStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def put_record(self, record: DeliveryRecord) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO deliveries (project_id, delivery_id, seq, body) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (project_id, delivery_id) DO UPDATE SET body = excluded.body",
                (
                    record.project_id,
                    record.delivery_id,
                    int(record.delivery_id[1:]),
                    _record_body(record),
                ),
            )

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
