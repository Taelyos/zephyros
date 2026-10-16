"""Both stores of the obstacles keep each obstacle as it was put, by project and number; the
SQLite one keeps them across a restart of the hub (ADR 0034)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zephyros.adapters.memory.obstacle_store import InMemoryObstacleStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.obstacle_store import SqliteObstacleStore
from zephyros.core.obstacles import Decision, Entry, Level, Obstacle, ObstacleStore, Status
from zephyros.core.ports.agent_engine import Side

T0 = datetime(2026, 10, 16, 8, 0, 0, 123456, tzinfo=UTC)


def obstacle(project: str = "p_abcdef12", number: int = 1) -> Obstacle:
    return Obstacle(
        project_id=project,
        number=number,
        author=f"contractor:{project}",
        side=Side.CONTRACTOR,
        nature="environment",
        blocking=True,
        title="pip cannot reach the registry",
        description="every install times out — the proxy answers 403",
        evidence=({"kind": "command", "command": "pip install x", "exit_code": 1},),
        tried="twice, ten minutes apart",
        needed="the registry allowed",
        reported_at=T0,
        status=Status.OPEN,
        level=Level.OWNER,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ObstacleStore]:
    if request.param == "memory":
        yield InMemoryObstacleStore()
        return
    database = Database(tmp_path / "hub.db")
    yield SqliteObstacleStore(database)
    database.close()


def test_an_obstacle_is_kept_and_replaced_by_project_and_number(store: ObstacleStore) -> None:
    first = obstacle()
    store.add(first)
    store.add(obstacle(number=2))
    store.add(obstacle("p_zzzzzz99"))
    assert store.get("p_abcdef12", 1) == first
    assert store.get("p_abcdef12", 3) is None
    answered = replace(
        first,
        status=Status.ESCALATED,
        level=Level.OPERATOR,
        entries=(Entry(Decision.ESCALATE, "owner:p_abcdef12", T0, Level.OWNER, "the proxy"),),
    )
    store.put(answered)
    assert store.get("p_abcdef12", 1) == answered
    assert [o.number for o in store.of_project("p_abcdef12")] == [1, 2]
    assert [o.project_id for o in store.of_project("p_zzzzzz99")] == ["p_zzzzzz99"]
    assert store.of_project("p_nothing00") == []


def test_a_number_is_never_taken_twice_nor_an_unknown_one_replaced(store: ObstacleStore) -> None:
    store.add(obstacle())
    with pytest.raises(Exception):  # noqa: B017, PT011  # reason: each store raises its own error
        store.add(obstacle())
    with pytest.raises(ValueError, match="no OB-007"):
        store.put(obstacle(number=7))


def test_the_sqlite_store_keeps_them_across_a_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "hub.db")
    SqliteObstacleStore(database).add(obstacle())
    database.close()
    again = Database(tmp_path / "hub.db")
    assert SqliteObstacleStore(again).get("p_abcdef12", 1) == obstacle()
    again.close()
