"""Both stores of the continuity keep each task as it was put, one per project and kind; the
SQLite one keeps it across a restart of the hub (T12)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zephyros.adapters.memory.continuity_store import InMemoryContinuityStore
from zephyros.adapters.sqlite.continuity_store import SqliteContinuityStore
from zephyros.adapters.sqlite.database import Database
from zephyros.core.continuity import (
    DECISION,
    DELIVERY,
    NO_ACCEPTANCE_TEST,
    ContinuityStore,
    Expectation,
    TaskState,
)

T0 = datetime(2026, 10, 12, 8, 0, 0, 123456, tzinfo=UTC)


def delivery(
    project: str = "p_abcdef12", *, step: int = 0, last: datetime = T0, held: str | None = None
) -> Expectation:
    state = TaskState(project, "contractor", DELIVERY, "the base of example/calc is ready", T0)
    return Expectation(state, last=last, step=step, held=held)


def decision(project: str = "p_abcdef12") -> Expectation:
    state = TaskState(project, "main-agent", DECISION, "change c_0001 awaits a decision", T0)
    return Expectation(state, last=T0 + timedelta(seconds=30), step=1)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ContinuityStore]:
    if request.param == "memory":
        yield InMemoryContinuityStore()
        return
    database = Database(tmp_path / "hub.db")
    yield SqliteContinuityStore(database)
    database.close()


def test_a_task_comes_back_as_it_was_put(store: ContinuityStore) -> None:
    held = delivery(step=2, last=T0 + timedelta(hours=1), held=NO_ACCEPTANCE_TEST)
    store.put(held)
    store.put(decision())
    assert store.all() == [decision(), held]  # in order of project, then kind


def test_a_project_keeps_one_task_of_each_kind(store: ContinuityStore) -> None:
    store.put(delivery(step=3))
    store.put(delivery(step=0, held=NO_ACCEPTANCE_TEST))
    store.put(delivery("p_zzzzzz99"))
    assert store.all() == [delivery(step=0, held=NO_ACCEPTANCE_TEST), delivery("p_zzzzzz99")]


def test_a_task_answered_is_gone_and_nothing_else(store: ContinuityStore) -> None:
    store.put(delivery())
    store.put(decision())
    store.remove("p_abcdef12", DELIVERY)
    store.remove("p_abcdef12", DELIVERY)  # twice is harmless
    store.remove("p_zzzzzz99", DECISION)  # so is a task that is not there
    assert store.all() == [decision()]


def test_the_database_keeps_the_tasks_across_a_restart(tmp_path: Path) -> None:
    before = Database(tmp_path / "hub.db")
    store = SqliteContinuityStore(before)
    store.put(delivery(step=1, held=NO_ACCEPTANCE_TEST))
    store.put(decision())
    before.close()
    after = Database(tmp_path / "hub.db")
    assert SqliteContinuityStore(after).all() == [
        decision(),
        delivery(step=1, held=NO_ACCEPTANCE_TEST),
    ]
    after.close()
