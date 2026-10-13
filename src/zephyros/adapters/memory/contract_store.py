"""The contract in memory."""

from __future__ import annotations

import dataclasses
from datetime import datetime

from zephyros.core.contract import ChangeStatus, ContractVersion, PendingChange


class InMemoryContractStore:
    def __init__(self) -> None:
        self._versions: dict[str, list[ContractVersion]] = {}
        self._pending: dict[tuple[str, str], PendingChange] = {}

    def latest(self, project_id: str) -> ContractVersion | None:
        versions = self._versions.get(project_id)
        return versions[-1] if versions else None

    def get(self, project_id: str, number: int) -> ContractVersion | None:
        for version in self._versions.get(project_id, []):
            if version.number == number:
                return version
        return None

    def numbers(self, project_id: str) -> list[int]:
        return [v.number for v in self._versions.get(project_id, [])]

    def append(self, project_id: str, version: ContractVersion) -> None:
        versions = self._versions.setdefault(project_id, [])
        expected = len(versions) + 1
        if version.number != expected:
            raise ValueError(f"expected version {expected}, got {version.number}")
        if any(existing.id == version.id for existing in versions):
            raise ValueError("a version with this identifier exists")
        versions.append(version)

    def add_pending(self, change: PendingChange) -> None:
        key = (change.project_id, change.id)
        if key in self._pending:
            raise ValueError("a change with this identifier exists")
        self._pending[key] = change

    def get_pending(self, project_id: str, change_id: str) -> PendingChange | None:
        return self._pending.get((project_id, change_id))

    def pending(self, project_id: str) -> list[PendingChange]:
        return [
            change
            for (project, _), change in self._pending.items()
            if project == project_id and change.status is ChangeStatus.PENDING
        ]

    def _resolve(
        self,
        project_id: str,
        change_id: str,
        status: ChangeStatus,
        by: str,
        at: datetime,
        version: ContractVersion | None,
    ) -> bool:
        change = self._pending.get((project_id, change_id))
        if change is None or change.status is not ChangeStatus.PENDING:
            return False
        if version is not None:
            self.append(project_id, version)
        self._pending[(project_id, change_id)] = dataclasses.replace(
            change,
            status=status,
            resolved_by=by,
            resolved_at=at,
            resulting_version=None if version is None else version.id,
        )
        return True

    def reject(self, project_id: str, change_id: str, by: str, at: datetime) -> bool:
        return self._resolve(project_id, change_id, ChangeStatus.REJECTED, by, at, None)

    def approve(
        self, project_id: str, change_id: str, by: str, at: datetime, version: ContractVersion
    ) -> bool:
        return self._resolve(project_id, change_id, ChangeStatus.APPROVED, by, at, version)
