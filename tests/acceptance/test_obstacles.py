"""ADR 0034 through a real hub and its channels: an obstacle goes up the chain by decision, each
step logged in the chain of the log; the main agent hears of what reaches the operator and the
human; the Contractor reads its own obstacles only, and no answer carries a hidden test to it;
the obstacles outlive a restart and appear in the evidence report.

The instances call on MCP from their addresses, the main agent on its endpoint, the operator on
its channel. Oracles: O-REFUSE (a refusal with its code), O-STATE (the obstacles unchanged), O-LOG
(one record names the caller and the action).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.test_delivery_loop import (
    ADD_SUB,
    BASE,
    REPOSITORY,
    deliver,
    make_world,
    prepared,
    status,
)
from tests.acceptance.world import Result, World
from tests.support.git_fixtures import make_forge_repository
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.core.audit import Action, Outcome, Reason

pytestmark = pytest.mark.acceptance

REPORT: dict[str, Any] = {
    "nature": "tool",
    "blocking": True,
    "title": "the bench announces a run and never starts it",
    "description": "bench run prints 'started' and exits 0; no run ever appears in its queue",
    "evidence": [
        {"kind": "command", "command": "bench run", "output": "started", "exit_code": 0},
        {"kind": "path", "path": "/opt/project-tools/bin/bench"},
    ],
    "tried": "five runs over ninety minutes, with and without --wait",
    "needed": "a bench that runs, or a way to know why it does not",
}
HIDDEN = (
    "import subprocess\n\n\ndef test_score():\n"
    "    out = subprocess.run(['bench', 'score'], capture_output=True, text=True)\n"
    "    assert out.returncode == 0, out.stderr\n"
    "    assert float(out.stdout) >= 0.87, 'the run scores at least 0.87 on the hidden track'\n"
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def looping(tmp_path: Path) -> Iterator[World]:
    """A world with the delivery loop: a directory forge, the git staging area, the
    assertions runner."""
    forge = Path(tempfile.mkdtemp(prefix="zf"))
    make_forge_repository(forge, REPOSITORY, {"main": BASE})
    try:
        yield make_world(tmp_path, forge, AssertionRunner())
    finally:
        shutil.rmtree(forge, ignore_errors=True)


def data(result: Result) -> dict[str, Any]:
    assert not result.refused, result.error
    assert result.data is not None
    return result.data


def records(world: World, action: Action) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (r.principal, r.outcome.value, json.loads(r.parameters))
        for r in world.records(world.project_a)
        if r.action is action
    ]


def obstacles_state(world: World) -> list[str]:
    """O-STATE: the obstacles as the hub keeps them."""
    service = world.hub._obstacles  # reason: the oracle looks under the hub
    assert service is not None
    return [repr(o) for o in service.of_project(world.project_a)]


def test_an_obstacle_goes_up_the_chain_by_decision(world: World) -> None:
    contractor, owner, main, operator = (
        world["contractor_a"],
        world["owner_a"],
        world["main"],
        world["operator"],
    )
    reported = data(contractor.tool("obstacle_report", REPORT))
    assert (reported["obstacle_id"], reported["awaits"]) == ("OB-001", "owner")

    # the Owner side finds it awaiting its answer, reads it, and escalates it
    waiting = data(owner.tool("obstacle_list", {"status": "awaiting", "level": "owner"}))
    assert [o["obstacle_id"] for o in waiting["obstacles"]] == ["OB-001"]
    read = data(owner.tool("obstacle_read", {"obstacle_id": "OB-001"}))
    assert read["evidence"][1] == {"kind": "path", "path": "/opt/project-tools/bin/bench"}
    escalated = data(
        owner.tool(
            "obstacle_answer",
            {
                "obstacle_id": "OB-001",
                "decision": "escalate",
                "text": "the bench is the operator's: neither side can change it",
            },
        )
    )
    assert escalated["awaits"] == "operator"

    # the main agent, within its rights, escalates it to the human
    for_operator = data(main.tool("obstacle_list", {"status": "awaiting", "level": "operator"}))
    assert [(o["project_id"], o["obstacle_id"]) for o in for_operator["obstacles"]] == [
        (world.project_a, "OB-001")
    ]
    data(
        main.tool(
            "obstacle_answer",
            {
                "project_id": world.project_a,
                "obstacle_id": "OB-001",
                "decision": "escalate",
                "text": "the machine of the bench needs a person",
            },
        )
    )
    # the human follows it without reading the log, and answers on the operator channel
    for_human = data(operator.tool("obstacle_list", {"status": "awaiting", "level": "human"}))
    assert for_human["count"] == 1
    data(
        operator.tool(
            "obstacle_answer",
            {
                "project_id": world.project_a,
                "obstacle_id": "OB-001",
                "decision": "handle",
                "action": "environment_fixed",
                "text": "the queue of the bench was full; it runs again",
            },
        )
    )

    # the Contractor reads every decision on its obstacle, and closes it
    answered = data(contractor.tool("obstacle_read", {"obstacle_id": "OB-001"}))
    assert [(d["decision"], d["level"]) for d in answered["decisions"]] == [
        ("escalate", "owner"),
        ("escalate", "operator"),
        ("handle", "human"),
    ]
    assert data(contractor.tool("obstacle_close", {"obstacle_id": "OB-001"}))["status"] == "closed"

    # each step is one record of the log, by the principal that took it
    world.run_notifications()
    principals = [(p, o) for p, o, _ in records(world, Action.OBSTACLE_ANSWER)]
    assert principals == [
        (f"owner:{world.project_a}", "ok"),
        ("main-agent", "ok"),
        ("operator", "ok"),
    ]
    sent = [(o, p["event"], p["level"]) for _, o, p in records(world, Action.NOTIFICATION)]
    assert sent == [("ok", "obstacle.raised", "operator"), ("ok", "obstacle.raised", "human")]
    world.verify_logs()


def test_the_contractor_never_reads_an_obstacle_of_the_owner_side(world: World) -> None:
    owner_side = dict(REPORT, description="the hidden track needs the bench at full speed")
    data(world["owner_a"].tool("obstacle_report", owner_side))
    before = obstacles_state(world)
    attempt = world["contractor_a"].tool("obstacle_read", {"obstacle_id": "OB-001"})
    assert attempt.refused  # O-REFUSE
    assert attempt.error is not None
    assert attempt.error["reason"] == Reason.FORBIDDEN.value
    assert data(world["contractor_a"].tool("obstacle_list", {}))["obstacles"] == []
    assert obstacles_state(world) == before  # O-STATE
    (refused,) = [r for r in records(world, Action.OBSTACLE_READ) if r[1] == "refused"]
    assert refused[0] == f"contractor:{world.project_a}"  # O-LOG
    # nor does an instance of another project, on its own token
    other = world["owner_b"].tool(
        "obstacle_read", {"project_id": world.project_a, "obstacle_id": "OB-001"}
    )
    assert other.error is not None
    assert other.error["reason"] == Reason.OTHER_PROJECT.value


def test_no_answer_carries_a_hidden_test_to_the_contractor(looping: World) -> None:
    world = looping
    data(world["owner_a"].tool("acceptance_put", {"path": "checks/score.py", "content": HIDDEN}))
    data(world["contractor_a"].tool("obstacle_report", dict(REPORT, nature="acceptance_test")))
    before = obstacles_state(world)
    leak = world["owner_a"].tool(
        "obstacle_answer",
        {"obstacle_id": "OB-001", "decision": "clarify", "text": "It reads:\n" + HIDDEN},
    )
    assert leak.error is not None
    assert leak.error["reason"] == Reason.ACCEPTANCE_LEAK.value
    assert obstacles_state(world) == before
    seen = data(world["contractor_a"].tool("obstacle_read", {"obstacle_id": "OB-001"}))
    assert seen["decisions"] == []
    assert "0.87" not in json.dumps(seen)


def test_the_obstacles_outlive_a_restart_of_the_hub(world: World) -> None:
    data(world["contractor_a"].tool("obstacle_report", REPORT))
    world.restart_hub()
    again = data(world["owner_a"].tool("obstacle_read", {"obstacle_id": "OB-001"}))
    assert again["title"] == REPORT["title"]
    second = data(world["contractor_a"].tool("obstacle_report", dict(REPORT, blocking=False)))
    assert second["obstacle_id"] == "OB-002"


def test_the_evidence_report_lists_the_obstacles(looping: World) -> None:
    world = looping
    prepared(world)
    data(world["contractor_a"].tool("obstacle_report", dict(REPORT, blocking=False)))
    data(
        world["owner_a"].tool(
            "obstacle_answer",
            {"obstacle_id": "OB-001", "decision": "reject", "text": "the bench is not needed"},
        )
    )
    data(world["owner_a"].tool("obstacle_report", dict(REPORT, title="a secret of the owner")))
    delivery = deliver(world, ADD_SUB)
    whole = status(world, delivery)["report"]
    assert "## Obstacles" in whole
    assert "| OB-001 | contractor | tool | no | rejected | reject by owner:" in whole
    assert "| OB-002 | owner | tool | yes | open, awaits the operator |" in whole
    of_contractor = data(world["contractor_a"].tool("delivery_status", {"delivery_id": delivery}))
    assert "| OB-001 |" in of_contractor["report"]
    assert "a secret of the owner" not in of_contractor["report"]
    assert "1 obstacle of the Owner side is kept for the Owner side." in of_contractor["report"]
    assert any(
        r.action is Action.GATE_DECISION and r.outcome is Outcome.OK
        for r in world.records(world.project_a)
    )
