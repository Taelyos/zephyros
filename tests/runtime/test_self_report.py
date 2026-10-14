"""CTR-14: Zephyros produces the evidence report of Zephyros on its own requirements.

The project of this run is Zephyros itself: the requirements are the real rows of the contract
(EXG-46, EXG-48, EXG-49), the acceptance tests are the Owner side's, the delivery is carried by
the simulated provider and the report says SIMULATION. The report replaces the hand-written
practice of the earlier milestones; set ZEPHYROS_SELF_REPORT to a path to keep a copy.
"""

from __future__ import annotations

import os
import re
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
REPOSITORY = "example/zephyros"
BRANCH = "report-wording"
# The acceptance tests are black box (ADR 0017, ATK-22): the kit does not import the delivered
# code, so a test reads `hub.VERSION` from a subprocess rather than importing it into the judge.
_READS_VERSION = (
    "    out = subprocess.run(\n"
    '        [sys.executable, "-c", "from hub import VERSION; print(bool(VERSION))"],\n'
    "        capture_output=True,\n"
    "        text=True,\n"
    "    )\n"
    '    assert out.stdout.strip() == "True", out.stderr\n'
)
BASE = {
    "hub.py": b"VERSION = '0.0.0'\n",
    "test_hub.py": (
        "import subprocess\nimport sys\n\n\ndef test_version():\n" + _READS_VERSION
    ).encode(),
}
ACCEPTANCE = (
    "import subprocess\n"
    "import sys\n\n\n"
    "def test_simulation_mark():\n" + _READS_VERSION + "\n\n"
    "def test_no_egress():\n" + _READS_VERSION + "\n\n"
    "def test_masking():\n" + _READS_VERSION
)
REQUIREMENTS = (
    # The real rows of the contract, verbatim.
    (
        "EXG-46",
        "P1",
        ("test",),
        ("test_report.py::test_simulation_mark",),
    ),
    (
        "EXG-48",
        "P1",
        ("test",),
        ("test_report.py::test_no_egress",),
    ),
    (
        "EXG-49",
        "P1",
        ("test",),
        ("test_report.py::test_masking",),
    ),
)


def requirements() -> list[dict[str, Any]]:
    objects = contract(
        (46, "P1", ("test",), ("test_report.py::test_simulation_mark",)),
        (48, "P1", ("test",), ("test_report.py::test_no_egress",)),
        (49, "P1", ("test",), ("test_report.py::test_masking",)),
    )
    return [wire(put(item)) for item in objects.values()]


def delivery_prompt() -> str:
    return "\n".join(
        [
            'RUN curl -fsS -H "Authorization: Bearer ${ZEPHYROS_TOKEN}" -o /work/base.bundle '
            '"${ZEPHYROS_HUB_URL%/}/staging/base"',
            "RUN git clone -q /work/base.bundle /work/repo",
            f"RUN git -C /work/repo checkout -qb {BRANCH}",
            "RUN printf 'NOTE = \"report\"\\n' >> /work/repo/hub.py",
            "RUN git -C /work/repo -c user.name=contractor "
            "-c user.email=contractor@example.invalid add -A",
            "RUN git -C /work/repo -c user.name=contractor "
            '-c user.email=contractor@example.invalid commit -qm "note"',
            f"RUN git -C /work/repo bundle create /work/delivery.bundle {BRANCH}",
            'RUN curl -fsS -H "Authorization: Bearer ${ZEPHYROS_TOKEN}" '
            '-H "content-type: application/x-git-bundle" --data-binary @/work/delivery.bundle '
            f'"${{ZEPHYROS_HUB_URL%/}}/staging/delivery?branch={BRANCH}"',
        ]
    )


def _delivery_id(deployment: Deployment, project: str) -> str:
    results = deployment.tool_results(project, CONTRACTOR)
    for content in reversed(results):
        found = re.search(r"delivery_id\D*?([A-Za-z0-9][A-Za-z0-9_.-]*)", content)
        if found:
            return found.group(1)
    first = "\n".join(content[:300] for content in results[:5])
    last = "\n".join(content[:300] for content in results[-3:])
    raise AssertionError(
        f"no delivery_id in the turn's tool results;\nfirst:\n{first}\nlast:\n{last}"
    )


def test_zephyros_produces_its_own_evidence_report(tmp_path: Path) -> None:
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
        project = str(deployment.data("project.create", name="Zephyros")["project_id"])
        deployment.projects.append(project)  # so that the teardown removes everything
        deployment.data(
            "contract.apply_reserved",
            project_id=project,
            summary="the contract of the project",
            operations=requirements(),
        )
        deployment.data("project.prepare", project_id=project, repository=REPOSITORY, branch="main")
        deployment.data(
            "acceptance.put", project_id=project, path="test_report.py", content=ACCEPTANCE
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            done = [r for r in deployment.records(project) if r.get("action") == "staging.prepare"]
            if done:
                assert done[-1].get("outcome") == "ok", done[-1]
                break
            time.sleep(2)
        else:
            raise AssertionError("the staging area was never prepared")

        ended = deployment.turn(project, CONTRACTOR, delivery_prompt(), seconds=420)
        assert ended == "completed", ended
        delivery_id = _delivery_id(deployment, project)
        deployment.turn(
            project,
            CONTRACTOR,
            f'TOOL mcp__zephyros__delivery_declare {{"delivery_id": "{delivery_id}"}}',
            seconds=180,
        )
        deadline = time.monotonic() + 240
        found: dict[str, Any] = {}
        while time.monotonic() < deadline:
            found = deployment.data("delivery.status", project_id=project, delivery_id=delivery_id)
            if found["state"] in ("proposed", "decided", "refused", "error"):
                break
            time.sleep(3)
        assert found["state"] == "proposed", found
        report = str(found["report"])

        # The report is the one of Zephyros on its own requirements, produced by Zephyros.
        assert "**Decision: PASS**" in report
        assert "**SIMULATION**" in report
        for requirement in ("EXG-46", "EXG-48", "EXG-49"):
            assert requirement in report
        assert "test_report.py::test_simulation_mark" in report

        destination = os.environ.get("ZEPHYROS_SELF_REPORT")
        if destination:
            Path(destination).write_text(report, encoding="utf-8")
    finally:
        deployment.down()
