"""ADR 0030: the tools of a project, mounted read-only on both sides and at the gate.

A whole Zephyros runs at the `sealed` level, with the `tests` kit, the directory forge and the
obedient stub as the model (ADR 0002); it runs twice, once with Hermes as the Contractor and once
with OpenCode. The tools are a small directory built here: a simulator compiled statically, the
same simulator linked against glibc and started through the loader and the libraries the
directory carries, and a Python script that adds up the scores of the runs. They are mounted in
an Owner turn (Hermes), a Contractor turn and the sandbox of verification: visible, runnable by
their name, and impossible to change. A delivery whose acceptance test plays the simulator, with
256 MiB of memory and a file in /tmp, passes the gate, and the report names the fingerprint that
`zephyros tools check` computes on the machine.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import ROOT, docker
from tests.support.git_fixtures import make_forge_repository
from tests.unit.core.contract_fixtures import put, wire
from tests.unit.core.test_gate import contract
from zephyros.adapters.project_tools import ToolsDirectory
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

OWNER, CONTRACTOR = Side.OWNER, Side.CONTRACTOR
REPOSITORY = "example/player"
SIMULATOR = r"""
#include <stdio.h>
#include <stdlib.h>

/* sim SEED [MIB]: touches MIB mebibytes, then prints the score of one run of SEED. */
int main(int argc, char **argv) {
    long seed = argc > 1 ? atol(argv[1]) : 1;
    long size = (argc > 2 ? atol(argv[2]) : 0) << 20;
    char *block = size ? malloc(size) : NULL;
    if (size && !block) {
        fputs("sim: no memory\n", stderr);
        return 3;
    }
    for (long at = 0; at < size; at += 4096) block[at] = (char)at;
    printf("sim seed=%ld score=%ld\n", seed, (seed * 7919) % 1000);
    return 0;
}
"""
BUILD = """set -eu
cd /out
mkdir -p bin lib libexec
gcc -O2 -static -o bin/sim /src/sim.c
gcc -O2 -o libexec/sim /src/sim.c
cp -L "$(ldd libexec/sim | awk '/ld-linux/ {print $1}')" lib/
for library in $(ldd libexec/sim | awk '/=> \\// {print $3}'); do cp -L "$library" lib/; done
"""
SCORE = """#!/usr/bin/env python3
\"\"\"Adds up the scores of the runs on its input.\"\"\"
import re
import sys

print("total", sum(int(found) for found in re.findall(r"score=(\\d+)", sys.stdin.read())))
"""
PORTABLE = """#!/bin/sh
# The simulator linked against glibc, with the loader and the libraries it was built against.
here=$(dirname "$(readlink -f "$0")")/..
exec "$here/lib/{loader}" --library-path "$here/lib" "$here/libexec/sim" "$@"
"""
ACCEPTANCE = """import errno
import os
import subprocess
import sys
import tempfile

TOOLS = "/opt/project-tools"


def run(*command, stdin=None):
    done = subprocess.run(list(command), input=stdin, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (command, done.stdout, done.stderr)
    return done.stdout


def test_the_simulator_scores_the_delivery():
    seed = run(sys.executable, "player.py").strip()
    played = run("sim", seed, "256")  # by its name, on the PATH, with 256 MiB touched
    portable = run("sim-glibc", seed)  # the glibc build, through the loader the tools carry
    assert played == portable == f"sim seed={seed} score={int(seed) * 7919 % 1000}\\n"
    assert run("score", stdin=played + portable).strip() == "total 1190"
    with tempfile.NamedTemporaryFile(dir="/tmp") as scratch:
        scratch.write(b"a run of the simulator")
        scratch.flush()
    with open("/proc/self/mountinfo") as mounts:
        (line,) = [m for m in mounts if m.split()[4] == TOOLS]
    assert "ro" in line.split()[5].split(",")
    assert not os.access(TOOLS + "/bin/sim", os.W_OK)
    try:
        open(TOOLS + "/written", "w")
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES)
    else:
        raise AssertionError("the tools could be written")
"""
TEST_ID = "test_tools.py::test_the_simulator_scores_the_delivery"
BASE = {"player.py": b"print(1)\n", "README.md": b"# player\n"}
PUSHED = "seed-five"
WRITES = (
    "touch /opt/project-tools/new",
    "mv /opt/project-tools/bin/sim /opt/project-tools/bin/sim.old",
    "chmod u+w /opt/project-tools/bin/score",
    "cp /work/tools-sim /opt/project-tools/bin/score",
)


def build_tools(root: Path) -> Path:
    """The tools, built in the pinned Hermes image, which has a C compiler: written by the user
    of the test, then given the modes an operator would give them."""
    source = root / "src"
    tools = root / "tools"
    source.mkdir()
    tools.mkdir()
    (source / "sim.c").write_text(SIMULATOR, encoding="utf-8")
    dockerfile = (ROOT / "images" / "instance" / "Dockerfile").read_text(encoding="utf-8")
    upstream = next(line.split()[1] for line in dockerfile.splitlines() if line.startswith("FROM "))
    docker(
        "run",
        "--rm",
        "--name",
        "zs-test-tools-build",
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{source}:/src:ro",
        "-v",
        f"{tools}:/out",
        "--entrypoint",
        "sh",
        upstream,
        "-c",
        BUILD,
    )
    loader = next(path.name for path in (tools / "lib").iterdir() if path.name.startswith("ld-"))
    (tools / "bin" / "sim-glibc").write_text(PORTABLE.format(loader=loader), encoding="utf-8")
    (tools / "bin" / "score").write_text(SCORE, encoding="utf-8")
    for path in [tools, *tools.rglob("*")]:
        path.chmod(0o755 if path.is_dir() or path.parent.name in ("bin", "libexec") else 0o644)
    (tools / "lib" / loader).chmod(0o755)
    return tools


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_tools(tmp_path_factory.mktemp("project-tools"))


@pytest.fixture(scope="module", params=["hermes", "opencode"])
def zephyros(
    request: pytest.FixtureRequest, tools: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[Deployment, str, Path]]:
    engine = str(request.param)
    forge = tmp_path_factory.mktemp(f"forge-{engine}")
    make_forge_repository(forge, REPOSITORY, {"main": BASE})
    deployment = Deployment(
        tmp_path_factory.mktemp(f"deployment-{engine}"),
        pool="10.227.0.0/16" if engine == "hermes" else "10.228.0.0/16",
        extra_config="[gateway]\nsimulation = true\n",
        env_extra={
            "ZEPHYROS_TOOLS": str(tools),
            "ZEPHYROS_EGRESS": "sealed",
            "ZEPHYROS_KIT": "tests",
            "ZEPHYROS_FORGE": "directory",
            "ZEPHYROS_FORGE_DIR": str(forge),
            "ZEPHYROS_CONTRACTOR_ENGINE": engine,
        },
    )
    deployment.up()
    try:
        yield deployment, engine, forge
    finally:
        deployment.down()


def step(engine: str, command: str) -> str:
    """One command of a turn, for the obedient stub: the terminal of Hermes, the bash tool of
    OpenCode."""
    if engine == "opencode":
        return "TOOL bash " + json.dumps({"command": command, "description": "a step of the test"})
    return f"RUN {command}"


def declare(engine: str, delivery_id: str) -> str:
    tool = (
        "zephyros_delivery_declare" if engine == "opencode" else "mcp__zephyros__delivery_declare"
    )
    return f"TOOL {tool} " + json.dumps({"delivery_id": delivery_id})


def try_the_tools(engine: str) -> str:
    """Run the tools by their name, then try to change them four ways; each exit code, and the
    line of the mount, go to /work."""
    return "\n".join(
        (
            step(engine, "sim 3 > /work/tools-sim 2>&1; sim-glibc 4 >> /work/tools-sim 2>&1"),
            step(engine, "command -v sim > /work/tools-where 2>&1"),
            step(engine, "sim 5 | score > /work/tools-score 2>&1"),
            *(
                step(engine, f"{attempt} 2>/dev/null; echo $? >> /work/tools-write")
                for attempt in WRITES
            ),
            step(engine, "grep ' /opt/project-tools ' /proc/self/mountinfo >> /work/tools-write"),
        )
    )


def tools_of_turns(deployment: Deployment, project: str) -> list[tuple[str, str]]:
    return [
        (str(r["parameters"]["side"]), str(r["parameters"].get("tools")))
        for r in deployment.records(project)
        if (r["action"], r["outcome"]) == ("turn.start", "ok")
    ]


@pytest.mark.parametrize("side", [OWNER, CONTRACTOR])
def test_adr_0030_a_turn_runs_the_tools_by_their_name_and_cannot_change_them(
    zephyros: tuple[Deployment, str, Path], tools: Path, side: Side
) -> None:
    deployment, engine, _forge = zephyros
    engine = "hermes" if side is OWNER else engine
    before = ToolsDirectory(tools).digest()
    project = deployment.project()
    assert deployment.turn(project, side, try_the_tools(engine)) == "completed"

    assert deployment.work_file(project, side, "tools-sim") == (
        "sim seed=3 score=757\nsim seed=4 score=676\n"
    )
    assert deployment.work_file(project, side, "tools-where") == "/opt/project-tools/bin/sim\n"
    assert deployment.work_file(project, side, "tools-score") == "total 595\n"
    written = (deployment.work_file(project, side, "tools-write") or "").splitlines()
    assert len(written) == len(WRITES) + 1, written
    assert all(code != "0" for code in written[: len(WRITES)]), written
    assert "ro" in written[-1].split()[5].split(","), written[-1]

    assert ToolsDirectory(tools).digest() == before
    assert tools_of_turns(deployment, project) == [(side.value, before.fingerprint)]
    checks = [r for r in deployment.records("hub") if r["action"] == "tools.check"]
    assert checks[-1]["parameters"]["tools"] == before.fingerprint


def wait_for(deployment: Deployment, project: str, action: str, seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = [r for r in deployment.records(project) if r["action"] == action]
        if found:
            return found[-1]
        time.sleep(2)
    raise AssertionError(f"no {action} in {seconds} s")


def deliver(engine: str) -> str:
    """The Contractor's work: the base from the staging area, the player changed to play seed 5,
    the branch pushed back."""
    hub = "${ZEPHYROS_HUB_URL%/}"
    auth = '-H "Authorization: Bearer $ZEPHYROS_TOKEN"'
    who = "-c user.name=contractor -c user.email=contractor@example.invalid"
    return "\n".join(
        step(engine, command)
        for command in (
            f'curl -fsS {auth} -o /work/base.bundle "{hub}/staging/base"',
            "git clone -q /work/base.bundle /work/repo",
            f"git -C /work/repo checkout -qb {PUSHED}",
            "printf 'print(5)\\n' > /work/repo/player.py",
            f"git -C /work/repo {who} commit -qam 'play seed five'",
            f"git -C /work/repo bundle create /work/delivery.bundle {PUSHED}",
            f'curl -fsS {auth} -H "content-type: application/x-git-bundle" '
            f'--data-binary @/work/delivery.bundle "{hub}/staging/delivery?branch={PUSHED}"',
        )
    )


def test_adr_0030_a_delivery_judged_with_the_tools_passes_the_gate_at_the_sealed_level(
    zephyros: tuple[Deployment, str, Path], tools: Path
) -> None:
    deployment, engine, _forge = zephyros
    measured = ToolsDirectory(tools).digest()
    objects = contract((1, "P0", ("test",), (TEST_ID,)))
    project = deployment.project([wire(put(item)) for item in objects.values()])
    deployment.data("project.prepare", project_id=project, repository=REPOSITORY, branch="main")
    deployment.data("acceptance.put", project_id=project, path="test_tools.py", content=ACCEPTANCE)
    assert wait_for(deployment, project, "staging.prepare", 120)["outcome"] == "ok"

    assert deployment.turn(project, CONTRACTOR, deliver(engine), seconds=420) == "completed"
    pushed = wait_for(deployment, project, "delivery.push", 10)
    assert pushed["outcome"] == "ok", pushed
    delivery_id = str(pushed["parameters"]["delivery_id"])
    assert deployment.turn(project, CONTRACTOR, declare(engine, delivery_id)) == "completed"

    found: dict[str, Any] = {}
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        found = deployment.data("delivery.status", project_id=project, delivery_id=delivery_id)
        if found["state"] in ("proposed", "decided", "refused", "error"):
            break
        time.sleep(3)
    assert (found["state"], found["decision"]) == ("proposed", "PASS"), (
        found.get("detail"),
        str(found.get("report"))[-3000:],
    )
    assert found["outcomes"] == {TEST_ID: "passed"}

    report = str(found["report"])
    assert (
        f"- Project tools: `{measured.fingerprint}` ({measured.files} files, {measured.bytes} "
        "bytes), mounted read-only at `/opt/project-tools` in the turns of both sides and in "
        "the sandbox"
    ) in report.splitlines()
    assert "found other tools" not in report
    runs = [r for r in deployment.records(project) if r["action"] == "verification.run"]
    assert [r["parameters"]["tools"] for r in runs] == [measured.fingerprint]
    assert {fingerprint for _side, fingerprint in tools_of_turns(deployment, project)} == {
        measured.fingerprint
    }
    # The hub, in its container, measured the tools as the machine does (`zephyros tools check`
    # runs this same code): a copy of them elsewhere can be checked against the report.
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", measured.fingerprint)
