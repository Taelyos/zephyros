"""ADR 0029: the wiring of a Contractor that runs OpenCode, proved without a model and without
OpenCode. A whole Zephyros runs (scripts/deploy.sh) with `instances.contractor.engine =
"opencode"`, and its OpenCode image is a stand-in: the real entrypoint and the real managed
configuration of images/instance-opencode, with a fake `opencode` that reads that configuration
as OpenCode does and calls the hub with it (tests/runtime/images/fake-opencode).

The hub holds a provider key of its own, a canary: the turn must not find it anywhere it can
look, in its environment or in any file it can read.
"""

from __future__ import annotations

import json
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import ROOT, build, docker
from tests.unit.core.contract_fixtures import seed_operations, wire
from zephyros.core.containers import VolumeKind, volume_name
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
CANARY = "sk-" + "zephyros" + "-canary-" + secrets.token_hex(12)
"""The provider key of the hub in this deployment. Its marker is never whole in this file."""
TURN_ENVIRONMENT = {
    "ZEPHYROS_HUB_URL",
    "ZEPHYROS_MODEL",
    "ZEPHYROS_TOKEN",
    "ZEPHYROS_REASONING_ECHO",
    "ZEPHYROS_CONTEXT_WINDOW",
    "ZEPHYROS_COMPACTION_TOKENS",
    "ZEPHYROS_SESSION",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
"""What the template gives a regulated turn (src/zephyros/core/containers.py)."""
IMAGE_ENVIRONMENT = {
    "PATH",
    "HOME",
    "HOSTNAME",
    "LANG",
    "GPG_KEY",
    "PYTHON_VERSION",
    "PYTHON_SHA256",
    "SHLVL",
    "PWD",
    "ZEPHYROS_REASONING_FIELD",
    "ZEPHYROS_OPENCODE_INPUT",
}
"""What the base image, the runtime and the shell of the entrypoint set: GPG_KEY is the public
key that signs the Python release, PYTHON_SHA256 its digest; ZEPHYROS_REASONING_FIELD and
ZEPHYROS_OPENCODE_INPUT are the entrypoint's, from ZEPHYROS_REASONING_ECHO (ADR 0032) and
ZEPHYROS_COMPACTION_TOKENS (ADR 0033)."""
MOUNTS = {
    "/",
    "/opt/data",
    "/work",
    "/tmp",
    "/etc/hosts",
    "/etc/hostname",
    "/etc/resolv.conf",
    "/sbin/docker-init",
}
"""The root, the two volumes, the temporary file system, the three files the runtime writes for
the network namespace of the guard, and the init process of the template. The pseudo-file
systems of the kernel (/proc, /dev, /sys) are left out."""
PSEUDO = ("/proc", "/dev", "/sys")


@pytest.fixture(scope="module")
def fake_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    context = tmp_path_factory.mktemp("fake-opencode")
    for source in (
        ROOT / "tests/runtime/images/fake-opencode/Dockerfile",
        ROOT / "tests/runtime/images/fake-opencode/fake_opencode.py",
        ROOT / "images/instance-opencode/entrypoint.sh",
        ROOT / "images/instance-opencode/opencode.jsonc",
    ):
        shutil.copy(source, context / source.name)
    return build("zephyros-test-fake-opencode:test", context)


@pytest.fixture(scope="module")
def zephyros(tmp_path_factory: pytest.TempPathFactory, fake_image: str) -> Iterator[Deployment]:
    state = tmp_path_factory.mktemp("deployment")
    key = state / "provider.key"
    key.write_text(CANARY + "\n", encoding="utf-8")
    deployment = Deployment(
        state,
        pool="10.223.0.0/16",
        env_extra={
            "ZEPHYROS_CONTRACTOR_ENGINE": "opencode",
            "ZEPHYROS_OPENCODE_IMAGE": fake_image,
            "ZEPHYROS_GATEWAY_KEY_FILE": str(key),
        },
    )
    deployment.up()
    try:
        yield deployment
    finally:
        deployment.down()


@pytest.fixture(scope="module")
def project(zephyros: Deployment) -> str:
    return zephyros.project([wire(o) for o in seed_operations()])


def report(zephyros: Deployment, project: str, turn: int) -> dict[str, Any]:
    text = zephyros.work_file(project, CONTRACTOR, f"fake-opencode-{turn}.json")
    assert text is not None, f"turn {turn} wrote no report"
    found: dict[str, Any] = json.loads(text)
    return found


def test_the_hub_holds_the_canary_key(zephyros: Deployment) -> None:
    """The control: the canary is really on the hub, where a turn must never find it."""
    seen = docker("exec", zephyros.hub, "cat", "/run/zephyros-credentials/gateway.key")
    assert seen.stdout.strip() == CANARY


def test_adr_0029_a_turn_runs_opencode_with_the_prompt_on_its_input_and_calls_the_hub(
    zephyros: Deployment, project: str
) -> None:
    prompt = f'-p looks like an option\nTOOL zephyros_contract_read {{"project_id": "{project}"}}'
    assert zephyros.turn(project, CONTRACTOR, prompt) == "completed"
    first = report(zephyros, project, 1)
    assert first["argv"] == ["run", "--continue"]
    assert first["session"] == [prompt + "\n"]
    assert first["gateway"]["status"] == 200
    [tool] = first["tools"]
    assert tool["status"] == 200
    assert tool["answer"]["result"]["isError"] is False
    assert "EXG-01" in tool["answer"]["result"]["content"][0]["text"]
    address = zephyros.data("instance.status", project_id=project, side="contractor")["address"]
    mine = [r for r in zephyros.records(project) if r["principal"] == f"contractor:{project}"]
    actions = [r["action"] for r in mine]
    assert "gateway.call" in actions  # the model call is counted against the instance
    assert "contract.read" in actions
    assert {r["parameters"]["source"] for r in mine} == {address}
    assert not [r for r in mine if r["outcome"] == "refused"]


def test_adr_0029_the_next_turn_continues_the_session_on_the_home_volume(
    zephyros: Deployment, project: str
) -> None:
    assert zephyros.turn(project, CONTRACTOR, "second turn") == "completed"
    second = report(zephyros, project, 2)
    assert second["turn"] == 2
    assert second["session"][-1] == "second turn\n"


def test_adr_0029_the_turn_holds_no_key_but_its_project_token(
    zephyros: Deployment, project: str
) -> None:
    """The environment of the turn is the template's and the image's, nothing else; it mounts
    the two volumes of the instance and nothing else; and the provider key of the hub is in no
    variable and no file the turn can read."""
    assert zephyros.turn(project, CONTRACTOR, "look around") == "completed"
    seen = report(zephyros, project, 3)
    assert set(seen["environment"]) <= TURN_ENVIRONMENT | IMAGE_ENVIRONMENT
    assert {"ZEPHYROS_HUB_URL", "ZEPHYROS_MODEL", "ZEPHYROS_TOKEN"} <= set(seen["environment"])
    secret_like = {
        name
        for name in seen["environment"]
        if any(word in name for word in ("SECRET", "PASSWORD", "CRED", "API_KEY", "AUTH"))
    }
    assert secret_like == set()
    points = {m["point"]: m for m in seen["mounts"] if not m["point"].startswith(PSEUDO)}
    assert set(points) <= MOUNTS, sorted(points)
    for path, kind in (("/opt/data", VolumeKind.HOME), ("/work", VolumeKind.WORK)):
        assert volume_name(project, CONTRACTOR, kind) in points[path]["root"], points[path]
    assert points["/tmp"]["type"] == "tmpfs"
    assert seen["marked"] == []
    assert "/run/zephyros-credentials/gateway.key" not in json.dumps(seen)


def test_adr_0029_the_stand_in_runs_the_real_files_of_the_image(fake_image: str) -> None:
    """The stand-in differs from the real image by the binary alone: the same entrypoint and
    the same managed configuration, byte for byte."""
    for path, source in (
        ("/opt/zephyros/entrypoint.sh", "images/instance-opencode/entrypoint.sh"),
        ("/etc/opencode/opencode.jsonc", "images/instance-opencode/opencode.jsonc"),
    ):
        inside = docker("run", "--rm", "--network", "none", "--entrypoint", "cat", fake_image, path)
        assert inside.stdout == Path(ROOT / source).read_text(encoding="utf-8")
