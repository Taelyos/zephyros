"""ADR 0034 on a whole Zephyros (scripts/deploy.sh), with the stub as the model.

The Contractor runs OpenCode and the Owner Hermes, so both engines are seen. The provider behind
the gateway is `recording_provider.py`, run in the hub container: it answers what the obedient
stub answers, so a turn does what its prompt says, and it tells whether each call of an agent
carried the base instruction of the images.

- A Contractor turn reports a blocking obstacle: the hub wakes the Owner side by itself, with a
  turn that names the obstacle. An Owner turn reads it and escalates it; the operator sees it
  awaiting and escalates it to the human; the event goes out.
- The Contractor reports two more; the Owner side tries to answer one with the code of its
  acceptance test (refused), rejects it, and answers the other with an amendment of the contract.
- The next Contractor turn is told of the decisions after its prompt, and reads them.
- Every call of either engine at work carried the base instruction.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker
from tests.unit.core.contract_fixtures import note, put, wire
from tests.unit.core.test_gate import contract
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

OWNER, CONTRACTOR = Side.OWNER, Side.CONTRACTOR
PORT = 9812
PROVIDER = (Path(__file__).parent / "recording_provider.py").read_text(encoding="utf-8")
INSTRUCTIONS = Path(__file__).parents[2] / "images" / "instance" / "instructions.md"
MARKER = "When something you cannot change stands in your way: report an obstacle"
HUB_PYTHON = "/opt/zephyros/bin/python"
# A ladder slow enough that the Owner side is woken once, and answers, before any restart.
CONTINUITY = "[continuity]\ninactivity_seconds = 600\nrestarts = 1\nunblock_seconds = 600\n"
HIDDEN = (
    "import subprocess\n\n\ndef test_score():\n"
    "    out = subprocess.run(['bench', 'score'], capture_output=True, text=True)\n"
    "    assert out.returncode == 0, out.stderr\n"
    "    assert float(out.stdout) >= 0.87, 'the run scores at least 0.87 on the hidden track'\n"
)
STATS = """
import sys, urllib.request
print(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())
"""


def report(nature: str, blocking: bool, title: str) -> dict[str, Any]:
    return {
        "nature": nature,
        "blocking": blocking,
        "title": title,
        "description": "the bench prints 'started' and exits 0; no run ever reaches its queue",
        "evidence": [
            {"kind": "command", "command": "bench run", "output": "started", "exit_code": 0},
            {"kind": "path", "path": "/opt/project-tools/bin/bench"},
        ],
        "tried": "five runs over ninety minutes, with and without --wait",
        "needed": "a bench that runs",
    }


def opencode(name: str, arguments: dict[str, Any]) -> str:
    return f"TOOL zephyros_{name} {json.dumps(arguments)}"


def hermes(name: str, arguments: dict[str, Any]) -> str:
    return f"TOOL mcp__zephyros__{name} {json.dumps(arguments)}"


@pytest.fixture(scope="module")
def key() -> str:
    return "sk-fake-" + secrets.token_hex(12)


@pytest.fixture(scope="module")
def zephyros(tmp_path_factory: pytest.TempPathFactory, key: str) -> Iterator[Deployment]:
    assert MARKER in INSTRUCTIONS.read_text(encoding="utf-8")
    state = tmp_path_factory.mktemp("deployment")
    key_file = tmp_path_factory.mktemp("key") / "gateway.key"
    key_file.write_text(key + "\n", encoding="utf-8")
    key_file.chmod(0o644)  # a fake key, read by the hub as uid 10001
    deployment = Deployment(
        state,
        pool="10.238.0.0/16",
        extra_config=(
            "[gateway]\n"
            'provider = "openai-compatible"\n'
            f'upstream_url = "http://127.0.0.1:{PORT}/v1"\n'
            'credential_file = "/run/zephyros-credentials/gateway.key"\n'
            'model = "served-model"\n'
            'upstream_model = "stub-model"\n'
            'models = ["stub-model"]\n'
            "reasoning_echo = false\n"
            "timeout_seconds = 60\n" + CONTINUITY
        ),
        env_extra={
            "ZEPHYROS_CONTRACTOR_ENGINE": "opencode",
            "ZEPHYROS_GATEWAY_KEY_FILE": str(key_file),
        },
    )
    deployment.up()
    try:
        docker("exec", "-d", deployment.hub, HUB_PYTHON, "-c", PROVIDER, str(PORT), key, MARKER)
        stats(deployment)  # it listens
        yield deployment
    finally:
        deployment.down()


def stats(zephyros: Deployment) -> dict[str, dict[str, int]]:
    for _ in range(50):
        done = docker(
            "exec",
            zephyros.hub,
            HUB_PYTHON,
            "-c",
            STATS,
            f"http://127.0.0.1:{PORT}/stats",
            check=False,
        )
        if done.returncode == 0:
            found: dict[str, dict[str, int]] = json.loads(done.stdout)
            return found
        time.sleep(0.2)
    raise AssertionError(f"the provider does not answer: {done.stderr[-500:]}")


def records(zephyros: Deployment, project: str, action: str) -> list[dict[str, Any]]:
    return [r for r in zephyros.records(project) if r.get("action") == action]


def wait_for(
    zephyros: Deployment, project: str, action: str, seconds: float, **matches: object
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for record in records(zephyros, project, action):
            parameters = record.get("parameters", {})
            if all(parameters.get(key) == value for key, value in matches.items()):
                return record
        time.sleep(3)
    raise AssertionError(f"no {action} matching {matches} in {seconds} s")


def wait_idle(zephyros: Deployment, project: str, side: Side, seconds: float = 300) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = zephyros.data("instance.status", project_id=project, side=side.value)
        if found["state"] in ("idle", "absent"):
            return str(found["last_turn"])
        time.sleep(2)
    raise AssertionError(f"the {side.value} of {project} did not become idle in {seconds} s")


def answered(zephyros: Deployment, project: str) -> list[tuple[str, str, str, str]]:
    """Every answer to an obstacle: who, what came of it, the decision, and the refusal."""
    return [
        (
            str(r["principal"]),
            str(r["outcome"]),
            str(r["parameters"].get("decision", "")),
            str(r["reason"]),
        )
        for r in records(zephyros, project, "obstacle.answer")
    ]


def test_adr_0034_an_obstacle_goes_up_the_chain_on_the_real_organs(zephyros: Deployment) -> None:
    objects = contract((1, "P0", ("test",), ("tests/test_score.py::test_score",)))
    project = zephyros.project([wire(put(item)) for item in objects.values()])
    owner_id = f"owner:{project}"

    # A Contractor turn (OpenCode) reports a blocking obstacle of a tool of the project.
    blocking = opencode("obstacle_report", report("tool", True, "the bench never starts a run"))
    assert zephyros.turn(project, CONTRACTOR, blocking) == "completed"
    reported = wait_for(zephyros, project, "obstacle.report", 30, obstacle_id="OB-001")
    assert reported["principal"] == f"contractor:{project}"
    assert reported["parameters"]["level"] == "owner"

    # The hub wakes the Owner side by itself, at once, with a turn that names the obstacle.
    woke = wait_for(
        zephyros, project, "continuity.step", 120, step="wake", task="obstacle", side="owner"
    )
    assert "OB-001" in woke["parameters"]["detail"]
    owner_start = wait_for(zephyros, project, "turn.start", 120, side="owner")
    assert owner_start["parameters"]["obstacles"] == ["OB-001"]  # its briefing names it
    wait_idle(zephyros, project, OWNER)
    prompts = [m["content"] for m in zephyros.messages(project, OWNER) if m["role"] == "user"]
    assert any("an obstacle awaits your answer" in p and "OB-001" in p for p in prompts)

    # An Owner turn (Hermes) reads it and escalates it to the operator.
    escalate = "\n".join(
        [
            hermes("obstacle_read", {"obstacle_id": "OB-001"}),
            hermes(
                "obstacle_answer",
                {
                    "obstacle_id": "OB-001",
                    "decision": "escalate",
                    "text": "the bench is the operator's: neither side can change it",
                },
            ),
        ]
    )
    assert zephyros.turn(project, OWNER, escalate) == "completed"
    assert answered(zephyros, project) == [(owner_id, "ok", "escalate", "none")]
    raised = wait_for(zephyros, project, "notification.sent", 60, event="obstacle.raised")
    assert raised["parameters"]["level"] == "operator"

    # The operator sees it awaiting, without reading the log, and escalates it to the human.
    waiting = zephyros.data("obstacle.list", status="awaiting", level="operator")["obstacles"]
    assert (project, "OB-001") in [(o["project_id"], o["obstacle_id"]) for o in waiting]
    zephyros.data(
        "obstacle.answer",
        project_id=project,
        obstacle_id="OB-001",
        decision="escalate",
        text="the machine of the bench needs a person",
    )
    wait_for(zephyros, project, "notification.sent", 60, event="obstacle.raised", level="human")
    for_human = zephyros.data("obstacle.list", status="awaiting", level="human")["obstacles"]
    assert (project, "OB-001") in [(o["project_id"], o["obstacle_id"]) for o in for_human]

    # The Contractor reports two more; the Owner side keeps a test, proposes an amendment.
    more = "\n".join(
        [
            opencode("obstacle_report", report("contract", False, "the score is not defined")),
            opencode("obstacle_report", report("acceptance_test", False, "the test needs a net")),
        ]
    )
    assert zephyros.turn(project, CONTRACTOR, more) == "completed"
    wait_for(zephyros, project, "obstacle.report", 30, obstacle_id="OB-003")
    proposal = "\n".join(
        [
            hermes("acceptance_put", {"path": "tests/test_score.py", "content": HIDDEN}),
            hermes(
                "contract_propose",
                {
                    "summary": "the score, defined",
                    "operations": [
                        wire(put(note(1, "EXG-01", "The score is the bench's, from 0 to 1.")))
                    ],
                },
            ),
        ]
    )
    assert zephyros.turn(project, OWNER, proposal) == "completed"
    (change,) = zephyros.data("contract.pending", project_id=project)["changes"]

    # It tries to answer with the code of its test (refused), rejects one, amends the other.
    answers = "\n".join(
        [
            hermes(
                "obstacle_answer",
                {"obstacle_id": "OB-003", "decision": "clarify", "text": "It reads:\n" + HIDDEN},
            ),
            hermes(
                "obstacle_answer",
                {
                    "obstacle_id": "OB-003",
                    "decision": "reject",
                    "text": "the test needs no network: your delivery calls the bench online",
                },
            ),
            hermes(
                "obstacle_answer",
                {
                    "obstacle_id": "OB-002",
                    "decision": "amend",
                    "change_id": change["change_id"],
                    "text": "the score is now defined in a note of the contract",
                },
            ),
        ]
    )
    assert zephyros.turn(project, OWNER, answers) == "completed"
    assert answered(zephyros, project)[1:] == [
        ("operator", "ok", "escalate", "none"),
        (owner_id, "refused", "", "acceptance_leak"),
        (owner_id, "ok", "reject", "none"),
        (owner_id, "ok", "amend", "none"),
    ]

    # The next Contractor turn is told of the decisions after its prompt, and reads them.
    read = "\n".join(
        [
            opencode("obstacle_read", {"obstacle_id": "OB-003"}),
            opencode("obstacle_read", {"obstacle_id": "OB-002"}),
        ]
    )
    assert zephyros.turn(project, CONTRACTOR, read) == "completed"
    briefed = [
        start["parameters"].get("obstacles")
        for start in records(zephyros, project, "turn.start")
        if start["parameters"]["side"] == "contractor"
    ]
    # told once of each decision: the escalations of OB-001 at its second turn, the answers
    # to OB-002 and OB-003 at its third
    assert briefed == [None, ["OB-001"], ["OB-002", "OB-003"]]
    reads = [
        r
        for r in records(zephyros, project, "obstacle.read")
        if r["principal"] == f"contractor:{project}"
    ]
    assert [(r["outcome"], r["parameters"]["status"]) for r in reads] == [
        ("ok", "rejected"),
        ("ok", "answered"),
    ]
    verified = zephyros.data("log.verify", stream=project)
    assert verified["ok"], verified

    # Every call of either engine at work carried the base instruction of its image.
    seen = stats(zephyros)
    for engine in ("hermes", "opencode"):
        assert seen[engine]["calls"] > 0, seen
        assert seen[engine]["instructed"] == seen[engine]["calls"], seen
