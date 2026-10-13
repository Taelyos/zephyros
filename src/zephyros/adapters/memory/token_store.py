"""Token records in memory."""

from __future__ import annotations

import dataclasses
from datetime import datetime

from zephyros.core.tokens import TokenRecord


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._records: dict[str, TokenRecord] = {}

    def add(self, record: TokenRecord) -> None:
        if record.token_id in self._records or any(
            existing.token_hash == record.token_hash for existing in self._records.values()
        ):
            raise ValueError("a token with this identifier or hash exists")
        self._records[record.token_id] = record

    def get_by_hash(self, token_hash: str) -> TokenRecord | None:
        for record in self._records.values():
            if record.token_hash == token_hash:
                return record
        return None

    def get(self, token_id: str) -> TokenRecord | None:
        return self._records.get(token_id)

    def revoke(self, token_id: str, at: datetime) -> bool:
        record = self._records.get(token_id)
        if record is None or record.revoked_at is not None:
            return False
        self._records[token_id] = dataclasses.replace(record, revoked_at=at)
        return True

    def for_instance(self, instance_id: str) -> list[TokenRecord]:
        return [r for r in self._records.values() if r.instance_id == instance_id]
