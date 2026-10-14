"""ATK-69 (EXG-19), through the hub: the anchor of the log, kept beside its database (ADR 0019).

Position: someone who reaches the data directory of the hub while it is stopped. Oracles: the
operator's `log.verify` reports the fault and names it, the hub records the state of the anchor
when it starts (`log.anchor`), and it goes on working. Positive control: before the attack, the
same verification passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.world import World
from zephyros.core.audit import Action, Outcome

pytestmark = pytest.mark.acceptance


def anchor_of(world: World) -> Path:
    return world.db_path.parent / "log-anchor.json"


def anchor_events(world: World) -> list[tuple[Outcome, dict[str, Any]]]:
    return [
        (record.outcome, json.loads(record.parameters))
        for record in world.records("hub")
        if record.action is Action.LOG_ANCHOR
    ]


def verify(world: World, stream: str) -> dict[str, Any]:
    return world.operator_tool("log.verify", stream=stream)


def test_atk_69_the_anchor_starts_with_the_hub_and_its_start_is_logged(world: World) -> None:
    assert anchor_of(world).exists()
    assert anchor_events(world) == [(Outcome.OK, {"anchor": "started"})]
    world.restart_hub()
    assert anchor_events(world) == [(Outcome.OK, {"anchor": "started"})]
    world.verify_logs()


def test_atk_69_a_deleted_anchor_is_reported_after_a_restart(world: World) -> None:
    world.operator_tool("project.get", project_id=world.project_a)
    assert verify(world, world.project_a)["ok"] is True  # positive control
    world.restart_hub()
    anchor_of(world).unlink()
    world.restart_hub()
    found = verify(world, world.project_a)
    assert (found["ok"], found["first_bad_seq"], found["problem"]) == (
        False,
        None,
        "the anchor kept outside the store is missing",
    )
    assert verify(world, "hub")["ok"] is False
    assert anchor_events(world)[-1] == (Outcome.ERROR, {"anchor": "missing"})
    # The hub goes on, and its next calls bring no new anchor with them.
    assert world.operator_tool("project.get", project_id=world.project_a)["name"] == "Project A"
    assert not anchor_of(world).exists()
    assert (
        verify(world, world.project_a)["problem"] == "the anchor kept outside the store is missing"
    )
