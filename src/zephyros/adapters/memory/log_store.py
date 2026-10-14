"""The audit log in memory."""

from __future__ import annotations

from collections.abc import Mapping

from zephyros.core.audit import AuditRecord, Head, LogError


class InMemoryLogStore:
    def __init__(self) -> None:
        self._records: dict[str, list[AuditRecord]] = {}
        self._heads: dict[str, Head] = {}

    def append(self, record: AuditRecord) -> None:
        records = self._records.setdefault(record.stream, [])
        expected = len(records) + 1
        if record.seq != expected:
            raise LogError(f"expected sequence number {expected}, got {record.seq}")
        records.append(record)

    def last(self, stream: str) -> AuditRecord | None:
        records = self._records.get(stream)
        return records[-1] if records else None

    def read(self, stream: str, after: int = 0, limit: int = 1000) -> list[AuditRecord]:
        return self._records.get(stream, [])[after : after + limit]

    def head(self, stream: str) -> Head | None:
        return self._heads.get(stream)

    def set_head(self, head: Head) -> None:
        self._heads[head.stream] = head

    def anchor(self) -> Mapping[str, Head] | None:
        return None  # a store in memory keeps no anchor outside itself

    def start_anchor(self) -> None:
        raise LogError("a store in memory keeps no anchor")

    def streams(self) -> list[str]:
        return sorted(self._records)
