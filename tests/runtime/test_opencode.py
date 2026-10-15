"""ADR 0029: the real OpenCode, pinned in images/instance-opencode, as the agent of a Contractor.
A whole Zephyros runs (scripts/deploy.sh) with `instances.contractor.engine = "opencode"`, and
the obedient stub is the model (ADR 0002): it calls whatever tool a line `TOOL <name> <json>` of
the prompt names, so no model is called and nothing rests on a model's choice. What a turn did
is read from the session database of OpenCode in the instance's home volume.

The configuration OpenCode resolves, and the list of its tools, are read from OpenCode itself,
offline: they are what tests/runtime/test_opencode_wiring.py and docs/instance-tools.md assume.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker
from tests.unit.core.contract_fixtures import seed_operations, wire
from zephyros.core.containers import VolumeKind, volume_name
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
IMAGE = "zephyros-instance-opencode:local"
TOKEN = "zph_c_" + "A" * 43
OFFLINE = (
    "-e",
    "ZEPHYROS_HUB_URL=http://10.0.0.3:8080",
    "-e",
    "ZEPHYROS_MODEL=deepseek-v4.1-flash",
    "-e",
    f"ZEPHYROS_TOKEN={TOKEN}",
)
ENGINE_TOOLS = (
    "bash",
    "edit",
    "glob",
    "grep",
    "question",
    "read",
    "skill",
    "task",
    "todowrite",
    "webfetch",
    "write",
)
"""The tools of the agent "build" of OpenCode with the managed configuration
(docs/instance-tools.md). `question` is denied in a session of `opencode run`."""

PARTS = """
import json, shutil, sqlite3
from pathlib import Path
source = Path('/opt/data/.local/share/opencode')
for name in ('opencode.db', 'opencode.db-wal', 'opencode.db-shm'):
    if (source / name).exists():
        shutil.copy(source / name, '/tmp/' + name)
db = sqlite3.connect('/tmp/opencode.db')
sessions = [row[0] for row in db.execute('select id from session where parent_id is null')]
parts = [json.loads(row[0]) for row in db.execute('select data from part order by id')]
print(json.dumps({'sessions': sessions, 'parts': parts}))
"""


@pytest.fixture(scope="module")
def zephyros(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    deployment = Deployment(
        tmp_path_factory.mktemp("deployment"),
        pool="10.224.0.0/16",
        env_extra={"ZEPHYROS_CONTRACTOR_ENGINE": "opencode"},
    )
    deployment.up()
    try:
        yield deployment
    finally:
        deployment.down()


@pytest.fixture(scope="module")
def image(zephyros: Deployment) -> str:
    """The OpenCode image that the deployment built and configured."""
    return docker("image", "inspect", "-f", "{{.Id}}", IMAGE).stdout.strip()


@pytest.fixture(scope="module")
def project(zephyros: Deployment) -> str:
    return zephyros.project([wire(o) for o in seed_operations()])


def offline(image: str, *words: str) -> str:
    """OpenCode in its image, without a network, as user 10000 with a read-only root."""
    done = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--tmpfs",
        "/opt/data:rw,size=64m,uid=10000,gid=10000",
        *OFFLINE,
        image,
        *words,
    )
    return done.stdout


def session(project: str) -> dict[str, Any]:
    """The root sessions of the instance and every part of every message, tool calls included."""
    done = docker(
        "run",
        "--rm",
        "--user",
        "10000:10000",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "--network",
        "none",
        "-v",
        f"{volume_name(project, CONTRACTOR, VolumeKind.HOME)}:/opt/data:ro",
        "--entrypoint",
        "python3",
        IMAGE,
        "-c",
        PARTS,
    )
    found: dict[str, Any] = json.loads(done.stdout)
    return found


def tool_outputs(project: str, tool: str) -> list[str]:
    return [
        str(part["state"].get("output", ""))
        for part in session(project)["parts"]
        if part.get("type") == "tool" and part.get("tool") == tool
    ]


def texts(project: str) -> list[str]:
    return [str(p.get("text", "")) for p in session(project)["parts"] if p.get("type") == "text"]


def bash(command: str) -> str:
    return "TOOL bash " + json.dumps({"command": command, "description": "a step of the test"})


# ----------------------------------------------------------------------------- offline
def test_adr_0029_the_image_runs_the_pinned_opencode(image: str) -> None:
    assert offline(image, "--version").strip() == "1.19.0"


def test_adr_0029_opencode_resolves_the_managed_configuration_to_the_hub_alone(image: str) -> None:
    """What OpenCode itself makes of /etc/opencode/opencode.jsonc: the gateway and the MCP server
    of the hub, the project token as their key (OpenCode masks it), one provider, nothing
    shared, no update, no plugin, every tool allowed."""
    config = json.loads(offline(image, "debug", "config"))
    assert config["enabled_providers"] == ["zephyros"]
    assert config["model"] == config["small_model"] == "zephyros/deepseek-v4.1-flash"
    assert list(config["provider"]) == ["zephyros"]
    options = config["provider"]["zephyros"]["options"]
    assert options["baseURL"] == "http://10.0.0.3:8080/v1"
    assert options["apiKey"] == "***"  # set, and masked by OpenCode
    assert list(config["mcp"]) == ["zephyros"]
    mcp = config["mcp"]["zephyros"]
    assert (mcp["type"], mcp["url"], mcp["oauth"]) == ("remote", "http://10.0.0.3:8080/mcp", False)
    assert config["share"] == "disabled"
    assert config["autoupdate"] is False
    assert config["plugin"] == []
    assert config["permission"] == {"*": "allow"}


def test_us_e1_07_the_documented_tool_list_matches_the_engine(image: str) -> None:
    agent = json.loads(offline(image, "debug", "agent", "build"))
    tools = tuple(sorted(name for name, on in agent["tools"].items() if on and name != "invalid"))
    assert tools == ENGINE_TOOLS


def test_adr_0029_the_image_holds_no_key(image: str) -> None:
    """The image carries no credential: no variable that names one, and no authentication file of
    OpenCode, which would hold a provider key."""
    environment = json.loads(docker("image", "inspect", "-f", "{{json .Config.Env}}", image).stdout)
    names = {item.split("=", 1)[0] for item in environment}
    assert not {n for n in names if any(w in n for w in ("SECRET", "PASSWORD", "TOKEN", "API_KEY"))}
    found = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "sh",
        image,
        "-c",
        "find / -xdev \\( -name auth.json -o -name '*.key' -o -name .netrc \\) 2>/dev/null; true",
    )
    assert found.stdout.strip() == ""


# ----------------------------------------------------------------------------- turns
def test_adr_0029_a_turn_of_opencode_uses_the_hub_from_the_address_of_its_guard(
    zephyros: Deployment, project: str
) -> None:
    prompt = "\n".join(
        (
            f'TOOL zephyros_contract_read {{"project_id": "{project}"}}',
            bash("echo control-oc1 > /work/control-oc1"),
        )
    )
    assert zephyros.turn(project, CONTRACTOR, prompt) == "completed"
    assert zephyros.work_file(project, CONTRACTOR, "control-oc1") == "control-oc1\n"
    assert any("EXG-01" in output for output in tool_outputs(project, "zephyros_contract_read"))
    address = zephyros.data("instance.status", project_id=project, side="contractor")["address"]
    mine = [r for r in zephyros.records(project) if r["principal"] == f"contractor:{project}"]
    actions = [r["action"] for r in mine]
    assert "gateway.call" in actions
    assert "contract.read" in actions
    assert {r["parameters"]["source"] for r in mine} == {address}
    assert not [r for r in mine if r["outcome"] == "refused"]


def test_adr_0029_the_next_turn_continues_the_same_session(
    zephyros: Deployment, project: str
) -> None:
    assert zephyros.turn(project, CONTRACTOR, bash("echo second-turn")) == "completed"
    assert len(session(project)["sessions"]) == 1
    # The stub counts the instructions of the whole conversation: those of the first turn too.
    assert any(text.startswith("STUB-DONE: 3 instruction(s)") for text in texts(project))


def test_atk_07_opencode_cannot_rewrite_its_configuration(
    zephyros: Deployment, project: str
) -> None:
    """The managed configuration is in the read-only root; a configuration of its own in its
    home, which points the model elsewhere, loses to it at the next turn."""
    own = json.dumps({"provider": {"zephyros": {"options": {"baseURL": "http://203.0.113.9/v1"}}}})
    prompt = "\n".join(
        (
            bash("echo '{}' > /etc/opencode/opencode.jsonc; echo control-07a"),
            bash(f"mkdir -p ~/.config/opencode && echo '{own}' > ~/.config/opencode/opencode.json"),
            bash("echo control-07b > /work/control-07b"),
        )
    )
    assert zephyros.turn(project, CONTRACTOR, prompt) == "completed"
    assert zephyros.work_file(project, CONTRACTOR, "control-07b") == "control-07b\n"
    assert any("Read-only file system" in output for output in tool_outputs(project, "bash"))
    before = len([r for r in zephyros.records(project) if r["action"] == "gateway.call"])
    assert zephyros.turn(project, CONTRACTOR, bash("echo after-07")) == "completed"
    after = len([r for r in zephyros.records(project) if r["action"] == "gateway.call"])
    assert after > before  # the next turn still talked to the gateway of the hub
