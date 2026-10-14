"""Fixtures of the attack catalog."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from tests.acceptance.world import World

CAPTURE_VARIABLE = "ZEPHYROS_REPLAY_CAPTURE"
"""When it names a directory, the hub database of every test is copied into it as <test>.db, so
the replay exporter can turn an attack of the catalog into a replay."""


def capture_world_database(source: Path, destination: Path) -> None:
    """Copy a hub database with the SQLite backup API, from a read-only connection.

    The hub still holds its own connection when the copy is taken: the backup API reads a
    consistent snapshot of the file, its write-ahead log included, without locking the hub out.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as reader,
        closing(sqlite3.connect(str(destination))) as writer,
    ):
        reader.backup(writer)


@pytest.fixture(autouse=True)
def replay_capture(request: pytest.FixtureRequest) -> Iterator[None]:
    """Copy the hub database of the test when ZEPHYROS_REPLAY_CAPTURE names a directory.

    Nothing happens when the variable is unset. The copy is taken after the test, so it holds
    everything the attack did. The world is looked up among the arguments of the test rather than
    through one fixture, because a suite may define its own `world` fixture (ATK-90 to ATK-93 do).
    """
    yield
    directory = os.environ.get(CAPTURE_VARIABLE)
    if not directory:
        return
    arguments = getattr(request.node, "funcargs", {})
    world = next(
        (value for value in arguments.values() if isinstance(value, World)),
        None,
    )
    if world is not None:
        capture_world_database(Path(world.db_path), Path(directory) / f"{request.node.name}.db")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture(params=["fresh", "after a restart of the hub"])
def restarted(request: pytest.FixtureRequest, tmp_path: Path) -> World:
    """A world, as it is and again after the hub restarts: the attacks of section 5.0 'repeat after
    restart'."""
    world = World(tmp_path)
    world.import_contract()
    if request.param != "fresh":
        world.restart_hub()
    return world
