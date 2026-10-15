"""ADR 0024 and ADR 0029: the real OpenCode as the Contractor of a sealed instance. A sealed guard
lets the instance reach the agent endpoint of the hub and nothing else, and counts what it
refuses. A turn of OpenCode, with the obedient stub as the model, does its work through the hub
and tries nothing else: the update check, the model list, the plugins and the language servers
that OpenCode would fetch by itself are off in its image, and the counters of the guard stay at
zero. The control: one attempt from a later turn, made on purpose, moves them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker
from tests.unit.core.contract_fixtures import seed_operations, wire
from zephyros.core.containers import guard_name
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
COUNTER = re.compile(r"counter packets (\d+) bytes \d+ (drop|reject)")


@pytest.fixture(scope="module")
def zephyros(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    deployment = Deployment(
        tmp_path_factory.mktemp("deployment"),
        pool="10.226.0.0/16",
        env_extra={"ZEPHYROS_CONTRACTOR_ENGINE": "opencode", "ZEPHYROS_EGRESS": "sealed"},
    )
    deployment.up()
    try:
        yield deployment
    finally:
        deployment.down()


def bash(command: str) -> str:
    return "TOOL bash " + json.dumps({"command": command, "description": "a step of the test"})


def refused(project: str) -> list[int]:
    """The packets each refusal rule of the guard's output chain has counted."""
    done = docker(
        "exec", guard_name(project, CONTRACTOR), "nft", "list", "chain", "inet", "zeph", "output"
    )
    counts = [int(packets) for packets, _ in COUNTER.findall(done.stdout)]
    assert len(counts) == 4, done.stdout  # protected IPv4, protected IPv6, TCP, the rest
    return counts


def test_adr_0029_a_sealed_turn_of_opencode_reaches_the_hub_and_tries_nothing_else(
    zephyros: Deployment,
) -> None:
    project = zephyros.project([wire(o) for o in seed_operations()])
    prompt = "\n".join(
        (
            f'TOOL zephyros_contract_read {{"project_id": "{project}"}}',
            bash("echo sealed > /work/sealed"),
        )
    )
    assert zephyros.turn(project, CONTRACTOR, prompt) == "completed"
    assert zephyros.work_file(project, CONTRACTOR, "sealed") == "sealed\n"
    records = zephyros.records(project)
    mine = [r for r in records if r["principal"] == f"contractor:{project}"]
    assert {"gateway.call", "contract.read"} <= {r["action"] for r in mine}
    assert not [r for r in mine if r["outcome"] == "refused"]
    assert not [r for r in records if r["action"] == "instance.egress"]
    assert refused(project) == [0, 0, 0, 0]

    # The control: an attempt made on purpose is refused and counted.
    attempt = bash("curl -s -m 5 http://203.0.113.9/ ; echo tried > /work/tried")
    assert zephyros.turn(project, CONTRACTOR, attempt) == "completed"
    assert zephyros.work_file(project, CONTRACTOR, "tried") == "tried\n"
    assert sum(refused(project)) > 0
    assert not [r for r in zephyros.records(project) if r["action"] == "instance.egress"]
