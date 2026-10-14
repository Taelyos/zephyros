"""Hub databases for the tests of the replay tools, written with the hub's own adapters.

The records go through the hub's `AuditLog`, so their hashes and their chain are the real ones;
the project, the contract versions and the deliveries go through the hub's SQLite stores. Time
comes from a clock the test moves. An instance's conversation is a small SQLite file with the
`messages` table of Hermes that the exporter reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.sqlite.contract_store import SqliteContractStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.adapters.sqlite.log_store import SqliteLogStore
from zephyros.adapters.sqlite.project_store import SqliteProjectStore
from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.clock import format_time
from zephyros.core.contract import ContractVersion, content_hash, version_id
from zephyros.core.deliveries import DeliveryRecord
from zephyros.core.projects import Project

Message = tuple[str, str, str, float]
"""One message of a conversation: session, role, content, time in seconds since the epoch."""


class HubFile:
    """One project's records, contract versions and deliveries, in a hub database file."""

    def __init__(self, path: Path, project: Project) -> None:
        self.project_id = project.id
        self.clock = FakeClock(project.created_at)
        self._database = Database(path)
        self._audit = AuditLog(SqliteLogStore(self._database), self.clock)
        self._contracts = SqliteContractStore(self._database)
        self._deliveries = SqliteDeliveryStore(self._database)
        SqliteProjectStore(self._database).add(project)

    def now(self) -> str:
        return format_time(self.clock.now())

    def log(
        self,
        wait: float,
        principal: str,
        action: Action,
        parameters: Mapping[str, object] | None = None,
        outcome: Outcome = Outcome.OK,
        reason: Reason = Reason.NONE,
    ) -> str:
        """Move the clock by `wait` seconds, then append one record; the record's time."""
        self.clock.advance(wait)
        event = AuditEvent(self.project_id, principal, action, outcome, reason, parameters)
        return self._audit.append(event).time

    def version(self, number: int, objects: Mapping[str, Mapping[str, object]], author: str) -> str:
        """Write version `number` of the contract, at the clock's time; its identifier."""
        digest = content_hash(objects)
        identifier = version_id(number, digest)
        parent = self._contracts.latest(self.project_id)
        self._contracts.append(
            self.project_id,
            ContractVersion(
                number=number,
                id=identifier,
                parent=None if parent is None else parent.id,
                content_hash=digest,
                objects=objects,
                author=author,
                time=self.clock.now(),
                operator_written=author == "operator",
            ),
        )
        return identifier

    def delivery(self, record: DeliveryRecord) -> None:
        """Write the row of a delivery, as it stands at the end of the run."""
        self._deliveries.put_record(record)

    def close(self) -> None:
        self._database.close()


def write_transcript(path: Path, messages: Sequence[Message]) -> Path:
    """An instance's state database, reduced to the table the exporter reads."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)"
        )
        db.executemany(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp) "
            "VALUES (?, ?, ?, NULL, NULL, ?)",
            messages,
        )
    return path
