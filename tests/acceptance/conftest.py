"""Fixtures of the attack catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.world import World


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
