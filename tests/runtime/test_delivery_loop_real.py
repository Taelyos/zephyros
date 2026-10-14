"""Z3 walking skeleton on the real organs: one delivery, end to end, with the obedient stub.

Hub, filter, guard, instance and sandbox are the real containers; the model is the scripted
stub, so nothing here talks to a provider. A real Hermes turn (the Contractor) fetches the
staging base over the agent endpoint, makes the change in its work volume, pushes the branch
back and — in a second turn, since one turn is one container (ADR 0016) — declares the
delivery. The hub replays the acceptance tests in a sandbox of the tests kit, computes the
gate, writes the evidence report and opens the pull request on the directory forge.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.support.git_fixtures import make_forge_repository
from tests.unit.core.contract_fixtures import put, wire
from tests.unit.core.test_gate import contract
from zephyros.adapters.git.run import git
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
REPOSITORY = "example/calc"
CANARY = "ghp_abcdefghijklmnopqrstuvwxyz012345"  # pragma: allowlist secret
BRANCH = f"add-sub-{CANARY}"  # ATK-84: a canary planted in a branch name, so it reaches the log
BASE = {
    ".gitignore": b"__pycache__/\n",
    "calc.py": b"def add(a, b):\n    return a + b\n",
    "test_calc.py": b"from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "README.md": b"# calc\n",
}
SUB_TEST = """from calc import sub

CANARY = "ghp_abcdefghijklmnopqrstuvwxyz012345"  # pragma: allowlist secret


def test_sub():
    print("acceptance run, token:", CANARY)  # ATK-84: planted in test output
    assert sub(3, 1) == 2
"""


def requirements() -> list[dict[str, Any]]:
    """EXG-01 (P0): add, verified by test_calc.py::test_add (in the repository).
    EXG-02 (P1): subtract, verified by test_sub.py::test_sub (the Owner side's new test)."""
    objects = contract(
        (1, "P0", ("test",), ("test_calc.py::test_add",)),
        (2, "P1", ("test",), ("test_sub.py::test_sub",)),
    )
    return [wire(put(item)) for item in objects.values()]


PUSHED = "add-sub"
"""The name the delivery goes under, once the hub refused BRANCH (ATK-84)."""


def delivery_prompt() -> str:
    """The Contractor's work, one instruction per line for the obedient stub. It first pushes
    its work under BRANCH, a name that holds a canary: the hub refuses it, and the second push,
    under a plain name, goes through."""
    return "\n".join(
        [
            'RUN curl -fsS -H "Authorization: Bearer $ZEPHYROS_TOKEN" -o /work/base.bundle '
            '"${ZEPHYROS_HUB_URL%/}/staging/base"',
            "RUN git clone -q /work/base.bundle /work/repo",
            f"RUN git -C /work/repo checkout -qb {PUSHED}",
            "RUN printf '\\n\\ndef sub(a, b):\\n    return a - b\\n' >> /work/repo/calc.py",
            # ATK-84: a canary planted in a delivered file
            "RUN printf 'token: sk-live-abcdefghijklmnop1234\\n' >> /work/repo/notes.txt",
            "RUN git -C /work/repo -c user.name=contractor "
            "-c user.email=contractor@example.invalid add -A",
            "RUN git -C /work/repo -c user.name=contractor "
            '-c user.email=contractor@example.invalid commit -qm "add sub"',
            f"RUN git -C /work/repo bundle create /work/delivery.bundle {PUSHED}",
            # ATK-84: a canary planted in a branch name; no -f, the refusal is expected
            'RUN curl -sS -o /work/refused.json -H "Authorization: Bearer $ZEPHYROS_TOKEN" '
            '-H "content-type: application/x-git-bundle" --data-binary @/work/delivery.bundle '
            f'"${{ZEPHYROS_HUB_URL%/}}/staging/delivery?branch={BRANCH}"',
            'RUN curl -fsS -H "Authorization: Bearer $ZEPHYROS_TOKEN" '
            '-H "content-type: application/x-git-bundle" --data-binary @/work/delivery.bundle '
            f'"${{ZEPHYROS_HUB_URL%/}}/staging/delivery?branch={PUSHED}"',
        ]
    )


def declare_prompt(delivery_id: str) -> str:
    return f'TOOL mcp__zephyros__delivery_declare {{"delivery_id": "{delivery_id}"}}'


def _delivery_id(deployment: Deployment, project: str) -> str:
    results = deployment.tool_results(project, CONTRACTOR)
    for content in reversed(results):
        found = re.search(r"delivery_id\D*?([A-Za-z0-9][A-Za-z0-9_.-]*)", content)
        if found:
            return found.group(1)
    shown = "\n".join(content[:300] for content in results[-4:])
    raise AssertionError(f"no delivery_id in the turn's tool results; last ones:\n{shown}")


def _wait_for_staging(deployment: Deployment, project: str, seconds: float = 120) -> None:
    """The preparation of the staging area is a job: wait for its record in the log."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        done = [r for r in deployment.records(project) if r.get("action") == "staging.prepare"]
        if done:
            assert done[-1].get("outcome") == "ok", done[-1]
            return
        time.sleep(2)
    raise AssertionError("the staging area was never prepared")


def _wait_for_decision(
    deployment: Deployment, project: str, delivery_id: str, seconds: float
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    found: dict[str, Any] = {}
    while time.monotonic() < deadline:
        found = deployment.data("delivery.status", project_id=project, delivery_id=delivery_id)
        if found["state"] in ("proposed", "decided", "refused", "error"):
            return found
        time.sleep(3)
    records = deployment.records(project)[-8:]
    raise AssertionError(
        f"no decision in {seconds} s; last status: {json.dumps(found, default=str)[:500]}; "
        f"last records: {json.dumps(records, default=str)[:1400]}"
    )


def test_one_delivery_from_push_to_pull_request_on_the_real_organs(tmp_path: Path) -> None:
    forge = tmp_path / "forge"
    forge.mkdir()
    make_forge_repository(forge, REPOSITORY, {"main": BASE})
    deployment = Deployment(
        tmp_path / "state",
        pool="10.219.0.0/16",
        extra_config="[gateway]\nsimulation = true\n",
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
        deployment.data("acceptance.put", project_id=project, path="test_sub.py", content=SUB_TEST)
        _wait_for_staging(deployment, project)

        ended = deployment.turn(project, CONTRACTOR, delivery_prompt(), seconds=420)
        assert ended == "completed", ended
        delivery_id = _delivery_id(deployment, project)
        # ATK-49: a branch pushed to the staging area opens nothing on the forge; the pull
        # request comes from the hub alone, once the delivery is decided.
        assert not (forge / "example" / "calc.pulls.json").exists()

        deployment.turn(project, CONTRACTOR, declare_prompt(delivery_id), seconds=180)

        found = _wait_for_decision(deployment, project, delivery_id, seconds=240)
        assert (found["state"], found["decision"]) == ("proposed", "PASS"), found.get("detail")
        assert found["outcomes"] == {
            "test_calc.py::test_add": "passed",
            "test_sub.py::test_sub": "passed",
        }
        assert found["changed_paths"] == ["calc.py", "notes.txt"]

        report = str(found["report"])
        # ATK-84, EXG-49: the canaries planted in the delivery (a delivered file, the output of
        # its test) come back masked — in the report, in the status and in the log store.
        for canary in (
            "sk-live-abcdefghijklmnop1234",  # pragma: allowlist secret (a canary of ATK-84)
            "ghp_abcdefghijklmnopqrstuvwxyz012345",  # pragma: allowlist secret (ATK-84)
        ):
            assert canary not in report
            assert canary not in json.dumps(deployment.records(project))
        # The push under BRANCH was refused, and the log tells it with the name masked; the
        # name reached neither the staging area nor the forge.
        refused = [
            r
            for r in deployment.records(project)
            if r.get("action") == "delivery.push" and r.get("outcome") == "refused"
        ]
        assert len(refused) == 1, refused
        assert "[masked]" in json.dumps(refused)
        assert "[masked]" in report
        assert "**Decision: PASS**" in report
        # EXG-46: the run is marked as a simulation, in the report and in the pull request
        assert "**SIMULATION**" in report
        assert (
            "| EXG-01 | `US-E1-01` | P0 | test | `test_calc.py::test_add` passed | yes |" in report
        )
        assert (
            "| EXG-02 | `US-E1-02` | P1 | test | `test_sub.py::test_sub` passed | yes |" in report
        )

        pulls = json.loads((forge / "example" / "calc.pulls.json").read_text())
        assert [(p["head"], p["base"]) for p in pulls] == [
            (f"zephyros/{delivery_id}-{PUSHED}", "main")
        ]
        assert "**SIMULATION**" in str(pulls[0]["body"])
        assert pulls[0]["body"] == report
        git(
            "--git-dir",
            str(forge / "example" / "calc.git"),
            "rev-parse",
            "--verify",
            f"zephyros/{delivery_id}-{PUSHED}",
            cwd=forge,
        )
    finally:
        deployment.down()
