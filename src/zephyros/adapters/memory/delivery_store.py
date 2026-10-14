"""Deliveries, acceptance tests and sources in memory. For tests and simulation."""

from __future__ import annotations

import threading
from dataclasses import replace

from zephyros.core.deliveries import DeliveryRecord, Source, StaleRecordError


class InMemoryDeliveryStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], DeliveryRecord] = {}
        self._acceptance: dict[str, dict[str, bytes]] = {}
        self._sources: dict[str, Source] = {}
        self._lock = threading.Lock()

    def put_record(self, record: DeliveryRecord) -> DeliveryRecord:
        key = (record.project_id, record.delivery_id)
        with self._lock:
            stored = self._records.get(key)
            if record.revision != (0 if stored is None else stored.revision):
                raise StaleRecordError(f"delivery {record.delivery_id} changed since it was read")
            written = replace(record, revision=record.revision + 1)
            self._records[key] = written
        return written

    def get_record(self, project_id: str, delivery_id: str) -> DeliveryRecord | None:
        with self._lock:
            return self._records.get((project_id, delivery_id))

    def records(self, project_id: str) -> list[DeliveryRecord]:
        with self._lock:
            found = [r for (p, _), r in self._records.items() if p == project_id]
        return sorted(found, key=lambda r: int(r.delivery_id[1:]))

    def put_acceptance(self, project_id: str, path: str, content: bytes) -> None:
        with self._lock:
            self._acceptance.setdefault(project_id, {})[path] = content

    def acceptance(self, project_id: str) -> dict[str, bytes]:
        with self._lock:
            return dict(self._acceptance.get(project_id, {}))

    def put_source(self, project_id: str, source: Source) -> None:
        with self._lock:
            self._sources[project_id] = source

    def get_source(self, project_id: str) -> Source | None:
        with self._lock:
            return self._sources.get(project_id)
