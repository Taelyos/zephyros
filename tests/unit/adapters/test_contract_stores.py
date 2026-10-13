"""Both contract stores behave the same; SQLite also refuses to rewrite a version."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.unit.core.contract_fixtures import (
    OWNER,
    PROJECT,
    change,
    note,
    put,
    seed_operations,
)
from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.contract_store import InMemoryContractStore
from zephyros.adapters.sqlite.contract_store import SqliteContractStore
from zephyros.adapters.sqlite.database import Database
from zephyros.core.contract import (
    ChangeStatus,
    ContractService,
    ContractStore,
    ContractVersion,
    PendingChange,
    content_hash,
    version_id,
)
from zephyros.core.principals import OPERATOR

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[ContractStore]:
    if request.param == "memory":
        yield InMemoryContractStore()
    else:
        database = Database(":memory:")
        yield SqliteContractStore(database)
        database.close()


def version(number: int, objects: dict[str, dict[str, object]] | None = None) -> ContractVersion:
    objects = objects or {}
    digest = content_hash(objects)
    return ContractVersion(
        number, version_id(number, digest), None, digest, objects, "operator", NOW, True
    )


def pending(change_id: str = "c_1") -> PendingChange:
    return PendingChange(
        id=change_id,
        project_id=PROJECT,
        proposer=OWNER.id,
        base_version=1,
        summary="s",
        operations=change(put(note(1, "EXG-01"))).operations,
        status=ChangeStatus.PENDING,
        created_at=NOW,
    )


def test_versions_are_appended_in_order_and_read_back(store: ContractStore) -> None:
    assert store.latest(PROJECT) is None
    assert store.get(PROJECT, 1) is None
    store.append(PROJECT, version(1, {"a": {"id": "a"}}))
    store.append(PROJECT, version(2, {"a": {"id": "a"}, "b": {"id": "b"}}))
    assert store.numbers(PROJECT) == [1, 2]
    assert store.numbers("p_other") == []
    latest = store.latest(PROJECT)
    assert latest is not None
    assert (latest.number, set(latest.objects)) == (2, {"a", "b"})
    first = store.get(PROJECT, 1)
    assert first is not None
    assert first.time == NOW
    assert first.operator_written


def test_a_version_out_of_order_or_repeated_is_refused(store: ContractStore) -> None:
    store.append(PROJECT, version(1))
    with pytest.raises(ValueError, match="expected version 2"):
        store.append(PROJECT, version(3))
    with pytest.raises(ValueError, match="expected version 2"):
        store.append(PROJECT, version(1))


def test_pending_changes_are_listed_until_resolved(store: ContractStore) -> None:
    store.add_pending(pending("c_1"))
    store.add_pending(pending("c_2"))
    assert [c.id for c in store.pending(PROJECT)] == ["c_1", "c_2"]
    stored = store.get_pending(PROJECT, "c_1")
    assert stored is not None
    assert stored.operations[0].object is not None
    assert store.get_pending(PROJECT, "c_9") is None
    assert store.get_pending("p_other", "c_1") is None
    assert store.reject(PROJECT, "c_1", "main-agent", NOW)
    assert not store.reject(PROJECT, "c_1", "main-agent", NOW)
    rejected = store.get_pending(PROJECT, "c_1")
    assert rejected is not None
    assert (rejected.status, rejected.resolved_by, rejected.resolved_at) == (
        ChangeStatus.REJECTED,
        "main-agent",
        NOW,
    )
    assert [c.id for c in store.pending(PROJECT)] == ["c_2"]


def test_approval_adds_the_version_and_closes_the_change_together(store: ContractStore) -> None:
    store.append(PROJECT, version(1))
    store.add_pending(pending())
    assert store.approve(PROJECT, "c_1", "main-agent", NOW, version(2))
    assert store.numbers(PROJECT) == [1, 2]
    done = store.get_pending(PROJECT, "c_1")
    assert done is not None
    assert (done.status, done.resulting_version) == (ChangeStatus.APPROVED, version(2).id)
    assert not store.approve(PROJECT, "c_1", "main-agent", NOW, version(3))
    assert store.numbers(PROJECT) == [1, 2]


def test_a_failed_approval_leaves_the_change_pending(store: ContractStore) -> None:
    store.append(PROJECT, version(1))
    store.add_pending(pending())
    with pytest.raises(ValueError, match="expected version 2"):
        store.approve(PROJECT, "c_1", "main-agent", NOW, version(7))
    still = store.get_pending(PROJECT, "c_1")
    assert still is not None
    assert still.status is ChangeStatus.PENDING
    assert store.numbers(PROJECT) == [1]


def test_a_duplicate_change_is_refused(store: ContractStore) -> None:
    store.add_pending(pending())
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        store.add_pending(pending())


def test_the_whole_flow_runs_the_same_on_both_stores(store: ContractStore) -> None:
    service = ContractService(store, FakeClock(), new_change_id=iter(["c_a", "c_b"]).__next__)
    service.apply_reserved(OPERATOR, PROJECT, change(*seed_operations()))
    proposal = service.propose(OWNER, PROJECT, change(put(note(2, "EXG-02"))))
    approved = service.approve(OPERATOR, PROJECT, proposal.id)
    assert approved.number == 2
    reread = service.read(OPERATOR, PROJECT, 2)
    assert reread.id == approved.id
    assert reread.objects == approved.objects


def test_sqlite_refuses_to_rewrite_or_delete_a_version() -> None:
    database = Database(":memory:")
    store = SqliteContractStore(database)
    store.append(PROJECT, version(1))
    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        database.transaction() as connection,
    ):
        connection.execute("UPDATE contract_versions SET author = 'someone'")
    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        database.transaction() as connection,
    ):
        connection.execute("DELETE FROM contract_versions")
    database.close()


def test_the_sqlite_contract_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    database = Database(path)
    service = ContractService(SqliteContractStore(database), FakeClock())
    first = service.apply_reserved(OPERATOR, PROJECT, change(*seed_operations()))
    pending_change = service.propose(OWNER, PROJECT, change(put(note(2, "EXG-02"))))
    database.close()
    reopened = Database(path)
    service = ContractService(SqliteContractStore(reopened), FakeClock())
    assert service.read(OPERATOR, PROJECT).id == first.id
    assert [c.id for c in service.pending(OPERATOR, PROJECT)] == [pending_change.id]
    assert dataclasses.is_dataclass(first)
    reopened.close()
