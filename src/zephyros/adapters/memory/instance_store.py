"""Instance records in memory. For tests and simulation."""

from __future__ import annotations

import threading

from zephyros.core.instances import InstanceRecord
from zephyros.core.ports.agent_engine import Side


class InMemoryInstanceStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, Side], InstanceRecord] = {}
        self._lock = threading.Lock()

    def get(self, project_id: str, side: Side) -> InstanceRecord | None:
        with self._lock:
            return self._records.get((project_id, side))

    def put(self, record: InstanceRecord) -> None:
        with self._lock:
            self._records[(record.project_id, record.side)] = record

    def remove(self, project_id: str, side: Side) -> None:
        with self._lock:
            self._records.pop((project_id, side), None)

    def all(self) -> list[InstanceRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: (r.project_id, r.side.value))
