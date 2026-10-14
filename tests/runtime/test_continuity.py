"""US-E7-01 on the real organs: the hub resumes a task that stays without activity.

The stack is the real one (hub, filter, guard, instances); only the model is the scripted
stub. A staging area is prepared and no Contractor turn ever happens: the hub, on its own,
wakes the instance with a turn that states the task, then restarts it — the two steps the
real ladder takes within the time the test waits for.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.support.git_fixtures import make_forge_repository
from tests.unit.core.contract_fixtures import put, wire
from tests.unit.core.test_gate import contract
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
REPOSITORY = "example/calc"
# The acceptance test is black box (ADR 0017, ATK-22): the kit does not import the delivered
# code, so it runs `calc` as a subprocess and reads its output.
ADD_TEST = """import subprocess
import sys


def test_add():
    out = subprocess.run(
        [sys.executable, "-c", "from calc import add; print(add(1, 2))"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "3", out.stderr
"""
BASE = {
    "calc.py": b"def add(a, b):\n    return a + b\n",
    "test_calc.py": ADD_TEST.encode(),
}

# The shortest ladder the configuration accepts: a 60 s wait per step.
CONTINUITY = "[continuity]\ninactivity_seconds = 60\nrestarts = 1\nunblock_seconds = 60\n"


def requirements() -> list[dict[str, Any]]:
    objects = contract((1, "P0", ("test",), ("test_calc.py::test_add",)))
    return [wire(put(item)) for item in objects.values()]


def _wait_for_record(
    deployment: Deployment, project: str, action: str, seconds: float, **matches: str
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for record in deployment.records(project):
            if record.get("action") != action:
                continue
            parameters = record.get("parameters", {})
            if all(parameters.get(key) == value for key, value in matches.items()):
                return record
        time.sleep(3)
    raise AssertionError(
        f"no {action} record matching {matches} in {seconds} s; all records: "
        f"{deployment.records(project)}"
    )


def _wait_for_count(
    deployment: Deployment, project: str, action: str, count: int, seconds: float
) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        seen = [r for r in deployment.records(project) if r.get("action") == action]
        if len(seen) >= count:
            return
        time.sleep(3)
    actions = [str(r.get("action")) for r in deployment.records(project)]
    raise AssertionError(f"less than {count} {action} records in {seconds} s: {actions}")


def test_the_hub_wakes_and_restarts_a_task_without_activity(tmp_path: Path) -> None:
    forge = tmp_path / "forge"
    forge.mkdir()
    make_forge_repository(forge, REPOSITORY, {"main": BASE})
    deployment = Deployment(
        tmp_path / "state",
        pool="10.220.0.0/16",
        extra_config=CONTINUITY,
        env_extra={
            "ZEPHYROS_KIT": "tests",
            "ZEPHYROS_FORGE": "directory",
            "ZEPHYROS_FORGE_DIR": str(forge),
        },
    )
    deployment.up()
    try:
        project = deployment.project(requirements())
        deployment.data("project.prepare", project_id=project, repository=REPOSITORY, branch="main")
        # the preparation of the staging area is a job: wait for its record
        _wait_for_record(deployment, project, "staging.prepare", seconds=120)

        # No Contractor turn ever happens. The hub wakes the instance by itself, with a turn
        # that states the task and what the hub keeps of it.
        woke = _wait_for_record(
            deployment, project, "continuity.step", seconds=150, step="wake", side="contractor"
        )
        assert woke.get("outcome") == "ok"
        started = _wait_for_record(deployment, project, "turn.start", seconds=90)
        assert started.get("parameters", {}).get("side") == "contractor"
        # the wake turn runs the obedient stub with the continuity prompt: it ends by itself
        _wait_for_record(deployment, project, "turn.end", seconds=180)

        # still no activity: the instance is restarted — its token is revoked, a fresh one is
        # issued and a fresh turn runs
        restarted = _wait_for_record(
            deployment, project, "continuity.step", seconds=150, step="restart"
        )
        assert restarted.get("outcome") == "ok"
        _wait_for_count(deployment, project, "token.revoke", 1, seconds=120)
        _wait_for_count(deployment, project, "turn.start", 2, seconds=120)
        _wait_for_count(deployment, project, "turn.end", 2, seconds=180)

        # still no activity: the ladder alerts the main agent, then the human (US-E7-01) —
        # the step is logged, and the notification goes out with it
        alerted = _wait_for_record(
            deployment, project, "continuity.step", seconds=210, step="alert_main"
        )
        assert alerted.get("outcome") == "ok"
        _wait_for_record(deployment, project, "notification.sent", seconds=150)
        human = _wait_for_record(
            deployment, project, "continuity.step", seconds=210, step="alert_human"
        )
        assert human.get("outcome") == "ok"
    finally:
        deployment.down()
