"""US-E7-01, as the operator sees it: the hub wakes the Contractor by itself only once the
contract is ready for it (ADR 0021).

The comparison of 2026-10-13 prepared the base first, for the Owner side to read it; 900 s
later the ladder woke the Contractor, which spent whole turns on a contract still being
written. Here the hub is the real one, with its delivery loop (git staging area, directory
forge) and its instance manager, which drives the filter in front of a fake container runtime.
The test plays the worker's beat; what it checks is what the operator reads: the log
(`continuity.step`, which also says when the task is held and why, and `turn.start`) and
`instance.status`.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.acceptance.world import MODEL, OPERATOR_PORT, World
from tests.support.fake_docker import FakeDocker
from tests.support.git_fixtures import make_forge_repository
from tests.unit.core.contract_fixtures import constraint, link, put, requirement, story, wire
from tests.unit.core.test_lifecycle import POLICY, TIMING
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.adapters.filter.server import build_filter_app
from zephyros.adapters.git.forge import DirectoryForge
from zephyros.adapters.http.agent import build_agent_app
from zephyros.adapters.http.main_agent import build_main_agent_app
from zephyros.adapters.http.operator import build_operator_app
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.instance_store import SqliteInstanceStore
from zephyros.app.hub import DeliverySetup, build_hub
from zephyros.core.audit import AuditLog
from zephyros.core.clock import Clock
from zephyros.core.containers import turn_name
from zephyros.core.continuity import Continuity
from zephyros.core.continuity import Timing as ContinuityTiming
from zephyros.core.instances import InstanceControl
from zephyros.core.lifecycle import InstanceManager
from zephyros.core.notifications import Notifier
from zephyros.core.ports.agent_engine import Side
from zephyros.core.tokens import TokenService

pytestmark = pytest.mark.acceptance

REPOSITORY = "example/calc"
BASE = {"calc.py": b"def add(a, b):\n    return a + b\n", "README.md": b"# calc\n"}
INACTIVITY = 900
NO_TEST = "hold: no_acceptance_test"
DECISION = "hold: pending_decision"


@dataclass
class LadderWorld(World):
    """The world of the attack catalog, with the ladder running behind the hub."""

    docker: FakeDocker = field(default_factory=FakeDocker)
    manager: InstanceManager | None = None

    def _boot(self) -> None:
        # Each boot is a new hub process: a new memory and a new manager. The database and the
        # containers outlive it, as they would on a host.
        if not self.docker.containers:
            self.docker.add_hub()
        journal = MemoryJournal()
        executor = Executor(self.docker.api(), POLICY, journal, clock=lambda: 1_000_000)
        broker = FilterClient(TestClient(build_filter_app(executor, journal)))
        continuity = Continuity(
            ContinuityTiming(inactivity_seconds=INACTIVITY, restarts=1, unblock_seconds=3600)
        )
        self.database = Database(self.db_path)
        timing = replace(TIMING, turn_seconds=10**6, silence_seconds=10**6)

        def manager(
            tokens: TokenService, audit: AuditLog, clock: Clock, notifier: Notifier
        ) -> InstanceControl:
            self.manager = InstanceManager(
                broker,
                SqliteInstanceStore(self.database),
                tokens,
                audit,
                clock,
                timing,
                sleep=self.clock.advance,
                continuity=continuity,
                notifier=notifier,
            )
            return self.manager

        runtime = build_hub(
            self.database,
            clock=self.clock,
            instance_factory=manager,
            model=MODEL,
            delivery=self.delivery,
            continuity=continuity,
        )
        self.hub, self.tokens, self.gateway = runtime.hub, runtime.tokens, runtime.gateway
        self.deliveries = runtime.deliveries
        self.agent_app = build_agent_app(self.hub, model_name=MODEL, gateway=self.gateway)
        self.main_app = build_main_agent_app(self.hub)
        self.operator_app = build_operator_app(self.hub, port=OPERATOR_PORT)
        assert self.manager is not None
        self.manager.reconcile()

    def beat(self, seconds: int) -> None:
        """Let time pass, then one round of the instance worker: the watchdog and the ladder,
        then the jobs they queued."""
        assert self.manager is not None
        self.clock.advance(seconds)
        self.manager.tick()
        while self.manager.step():
            pass

    def log(self, project: str) -> list[dict[str, Any]]:
        lines = self.operator_tool("log.read", stream=project, limit=500)["records"]
        return [json.loads(line) for line in lines]

    def story(self, project: str, side: str = "contractor") -> list[str]:
        """What the log says of the task of one side: each step of its ladder, and each time
        it was held (with why) or released."""
        told = []
        for record in self.log(project):
            parameters = record["parameters"]
            if record["action"] != "continuity.step" or parameters["side"] != side:
                continue
            step = parameters["step"]
            told.append(f"{step}: {parameters['why']}" if step == "hold" else step)
        return told

    def turns(self, project: str) -> list[str]:
        """The side of each turn started, from the log."""
        return [
            record["parameters"]["side"]
            for record in self.log(project)
            if record["action"] == "turn.start"
        ]

    def status(self, project: str, side: str = "contractor") -> str:
        return str(self.operator_tool("instance.status", project_id=project, side=side)["state"])


@pytest.fixture
def forge_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="zf"))
    make_forge_repository(root, REPOSITORY, {"main": BASE})
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def world(tmp_path: Path, forge_root: Path) -> LadderWorld:
    setup = DeliverySetup(
        staging_root=tmp_path / "staging",
        runner=AssertionRunner(),
        forge=DirectoryForge(forge_root),
        kit="assertions",
    )
    (tmp_path / "world").mkdir()
    # the Owner side calls for hours of the test clock: its token outlives them
    return LadderWorld(tmp_path / "world", token_lifetime=86_400, delivery=setup)


def the_need(world: LadderWorld, project: str) -> None:
    """What the operator imports first: the client's need and its constraint, no story yet."""
    world.operator_tool(
        "contract.apply_reserved",
        project_id=project,
        summary="the client's need and its constraint",
        operations=[
            wire(put(requirement(1, "The system shall subtract.", priority="P1"))),
            wire(put(constraint(1, priority="P0"))),
        ],
    )


def base_prepared(world: LadderWorld, project: str) -> None:
    world.operator_tool("project.prepare", project_id=project, repository=REPOSITORY, branch="main")
    world.run_jobs()
    prepared = [r for r in world.log(project) if r["action"] == "staging.prepare"]
    assert prepared[-1]["outcome"] == "ok"


def owner_writes_the_stories(world: LadderWorld, project: str, owner: str = "owner_a") -> str:
    """The Owner side proposes its story, its criterion and the test that verifies it."""
    proposal = world[owner].tool(
        "contract_propose",
        {
            "project_id": project,
            "summary": "the story of EXG-01 and its acceptance test",
            "operations": [
                wire(put(story(1, 1))),
                wire(put(link(1, 1, "EXG-01"))),
                {
                    "op": "put",
                    "object": {
                        "type": "criterion",
                        "id": "US-E1-01.1",
                        "story": "US-E1-01",
                        "text": "The system shall give a - b.",
                        "test": "checks/sub.txt::test_sub",
                    },
                },
            ],
        },
    )
    assert not proposal.refused, proposal.error
    assert proposal.data is not None
    put_test = world[owner].tool(
        "acceptance_put",
        {"project_id": project, "path": "checks/sub.txt", "content": "test_sub: pass\n"},
    )
    assert not put_test.refused, put_test.error
    return str(proposal.data["change_id"])


def owner_works(world: LadderWorld, project: str, rounds: int, owner: str = "owner_a") -> None:
    """The Owner side is in its turn: it calls the hub every ten minutes."""
    for _ in range(rounds):
        world.beat(600)
        assert not world[owner].tool("contract_read", {"project_id": project}).refused


def test_the_contractor_is_woken_only_once_its_contract_is_ready(world: LadderWorld) -> None:
    project = world.project_a
    the_need(world, project)
    base_prepared(world, project)
    # The Owner side reads the base and writes for longer than the inactivity of a task.
    owner_works(world, project, rounds=4)
    # the log says why nothing happens: the task waits for a test, the ladder did not stop
    assert world.story(project) == [NO_TEST]
    change_id = owner_writes_the_stories(world, project)
    owner_works(world, project, rounds=3)
    # proposed, not decided: the contract is still a draft
    assert world.story(project) == [NO_TEST, DECISION]
    assert world.status(project) == "absent"
    world.operator_tool("contract.approve", project_id=project, change_id=change_id)
    # The contract is ready. The ladder counts from the decision.
    world.beat(INACTIVITY - 1)
    assert world.story(project) == [NO_TEST, DECISION, "release"]
    world.beat(1)
    assert world.story(project) == [NO_TEST, DECISION, "release", "wake"]
    assert world.turns(project) == ["contractor"]
    assert world.status(project) == "turn"


def test_a_change_proposed_during_a_turn_holds_the_ladder_until_it_is_decided(
    world: LadderWorld,
) -> None:
    project = world.project_a
    the_need(world, project)
    base_prepared(world, project)
    world.operator_tool(
        "contract.approve", project_id=project, change_id=owner_writes_the_stories(world, project)
    )
    world.beat(INACTIVITY)
    woken = [NO_TEST, DECISION, "release", "wake"]
    assert world.story(project) == woken
    # While the woken turn runs, the Owner side proposes a change; the turn is not stopped.
    note = {"type": "note", "id": "NB-001", "target": "EXG-01", "text": "Integers only."}
    change = world["owner_a"].tool(
        "contract_propose",
        {"project_id": project, "summary": "a note", "operations": [{"op": "put", "object": note}]},
    )
    assert not change.refused, change.error
    assert change.data is not None
    world.beat(60)
    assert world.status(project) == "turn"
    world.docker.finish(turn_name(project, Side.CONTRACTOR))  # the turn ends with no delivery
    owner_works(world, project, rounds=4)
    # no restart, no alert while the contract is being changed
    assert world.story(project) == [*woken, DECISION]
    assert world.status(project) == "idle"
    world.operator_tool("contract.approve", project_id=project, change_id=change.data["change_id"])
    world.beat(INACTIVITY)
    # the contract the Contractor is to build changed: the ladder starts again, from the wake
    assert world.story(project) == [*woken, DECISION, "release", "wake"]
    assert world.turns(project) == ["contractor", "contractor"]


def test_each_project_wakes_its_contractor_for_its_own_contract(world: LadderWorld) -> None:
    ready, draft = world.project_a, world.project_b
    for project in (ready, draft):
        the_need(world, project)
        base_prepared(world, project)
    world.operator_tool(
        "contract.approve", project_id=ready, change_id=owner_writes_the_stories(world, ready)
    )
    world.beat(INACTIVITY)
    assert world.story(ready) == [NO_TEST, DECISION, "release", "wake"]
    assert world.story(draft) == [NO_TEST]
    assert world.status(draft) == "absent"


def test_after_a_restart_the_hub_wakes_no_contractor_on_what_it_forgot(
    world: LadderWorld,
) -> None:
    """The ladder lives in the hub's memory. A restart forgets the task of a base prepared
    before it: nothing is woken on it, even once its contract is ready. The next preparation
    of the base starts the ladder again."""
    project = world.project_a
    the_need(world, project)
    base_prepared(world, project)
    world.restart_hub()
    owner_works(world, project, rounds=4)
    world.operator_tool(
        "contract.approve", project_id=project, change_id=owner_writes_the_stories(world, project)
    )
    world.beat(INACTIVITY * 4)
    assert world.story(project) == [NO_TEST]  # said before the restart; nothing since
    assert world.status(project) == "absent"
    base_prepared(world, project)
    world.beat(INACTIVITY)
    assert world.story(project) == [NO_TEST, "wake"]
