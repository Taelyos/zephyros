"""ADR 0016 (amended) and ADR 0033, on a whole Zephyros (scripts/deploy.sh), with the obedient
stub as the model (ADR 0002). The Owner runs Hermes, the Contractor OpenCode.

- A new session: the operator starts a turn with `"session": "new"`; that turn and the next one
  work in a session of their own, the earlier one left behind, for Hermes and for OpenCode, and
  each start of a turn records which.
- The context of the agents: the window and the compaction point the configuration gives
  (1 000 000 tokens, 400 000 for the agents) reach every turn, and each engine reads them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker
from zephyros.core.containers import VolumeKind, volume_name
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

OWNER, CONTRACTOR = Side.OWNER, Side.CONTRACTOR
OPENCODE_IMAGE = "zephyros-instance-opencode:local"
TOKEN = "zph_c_" + "A" * 43

HERMES_SESSIONS = """
import json, sqlite3
db = sqlite3.connect('file:/opt/data/state.db?mode=ro', uri=True)
rows = db.execute(
    "select s.title, m.content from sessions s join messages m on m.session_id = s.id "
    "where m.role = 'user' order by m.id"
).fetchall()
print(json.dumps(rows))
"""

OPENCODE_SESSIONS = """
import json, shutil, sqlite3
from pathlib import Path
source = Path('/opt/data/.local/share/opencode')
for name in ('opencode.db', 'opencode.db-wal', 'opencode.db-shm'):
    if (source / name).exists():
        shutil.copy(source / name, '/tmp/' + name)
db = sqlite3.connect('/tmp/opencode.db')
roots = [r[0] for r in db.execute(
    'select id from session where parent_id is null order by time_created, id')]
texts = []
for session_id, data in db.execute(
        'select session_id, data from part order by id'):
    part = json.loads(data)
    if part.get('type') == 'text' and 'MARK-' in part.get('text', ''):
        texts.append([session_id, part['text'].strip()])
print(json.dumps({'roots': roots, 'texts': texts}))
"""

HERMES_READS = """
import json
from hermes_cli.config import load_config
from run_agent import AIAgent
cfg = load_config()
model = cfg["model"]
agent = AIAgent(model=model["default"], api_key=model["api_key"], base_url=model["base_url"],
                provider=model["provider"], quiet_mode=True, save_trajectories=False,
                platform="cli")
compressor = agent.context_compressor
print(json.dumps([compressor.context_length, compressor.threshold_tokens]))
"""
"""What Hermes makes of its configuration, offline: the window and the compaction point."""


@pytest.fixture(scope="module")
def zephyros(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    deployment = Deployment(
        tmp_path_factory.mktemp("deployment"),
        pool="10.238.0.0/16",
        extra_config=(
            "[gateway]\nsimulation = true\ncontext_window = 1000000\ncontext_tokens = 400000\n"
        ),
        env_extra={"ZEPHYROS_CONTRACTOR_ENGINE": "opencode"},
    )
    deployment.up()
    try:
        yield deployment
    finally:
        deployment.down()


def read_home(project: str, side: Side, image: str, entrypoint: str, script: str) -> Any:
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
        f"{volume_name(project, side, VolumeKind.HOME)}:/opt/data:ro",
        "--entrypoint",
        entrypoint,
        image,
        "-c",
        script,
    )
    return json.loads(done.stdout)


def turn_starts(zephyros: Deployment, project: str, side: Side) -> list[dict[str, Any]]:
    return [
        r["parameters"]
        for r in zephyros.records(project)
        if r["action"] == "turn.start"
        and r["outcome"] == "ok"
        and r["parameters"].get("side") == side.value
    ]


# ----------------------------------------------------------------------------- session (ADR 0016)
def test_adr_0016_hermes_starts_a_new_session_and_the_next_turn_continues_it(
    zephyros: Deployment,
) -> None:
    project = zephyros.project()
    assert zephyros.turn(project, OWNER, "MARK-1\nRUN echo one") == "completed"
    assert zephyros.turn(project, OWNER, "MARK-2\nRUN echo two", session="new") == "completed"
    assert zephyros.turn(project, OWNER, "MARK-3\nRUN echo three") == "completed"
    rows = read_home(
        project, OWNER, zephyros.instance_image(), "/opt/hermes/.venv/bin/python", HERMES_SESSIONS
    )
    marks = [(title, content.split("\n")[0]) for title, content in rows if "MARK-" in content]
    assert marks == [
        ("zephyros", "MARK-1"),
        ("zephyros #2", "MARK-2"),
        ("zephyros #2", "MARK-3"),
    ]
    assert [s["session"] for s in turn_starts(zephyros, project, OWNER)] == [
        "continue",
        "new",
        "continue",
    ]


def test_adr_0016_opencode_starts_a_new_session_and_the_next_turn_continues_it(
    zephyros: Deployment,
) -> None:
    project = zephyros.project()
    for mark, session in (("MARK-1", None), ("MARK-2", "new"), ("MARK-3", None)):
        prompt = f"{mark}\nTOOL bash " + json.dumps(
            {"command": f"echo {mark}", "description": "a step of the test"}
        )
        assert zephyros.turn(project, CONTRACTOR, prompt, session=session) == "completed"
    found = read_home(project, CONTRACTOR, OPENCODE_IMAGE, "python3", OPENCODE_SESSIONS)
    first, second = found["roots"]  # two conversations, the first one left behind
    by_mark = {text.split("\n")[0]: session_id for session_id, text in found["texts"]}
    assert by_mark["MARK-1"] == first
    assert by_mark["MARK-2"] == by_mark["MARK-3"] == second
    assert [s["session"] for s in turn_starts(zephyros, project, CONTRACTOR)] == [
        "continue",
        "new",
        "continue",
    ]


# ----------------------------------------------------------------------------- context (ADR 0033)
def test_adr_0033_every_turn_carries_and_records_the_context_of_the_configuration(
    zephyros: Deployment,
) -> None:
    project = zephyros.project()
    assert zephyros.turn(project, OWNER, "RUN echo context") == "completed"
    [record] = turn_starts(zephyros, project, OWNER)
    assert (
        record["context_window"],
        record["context_tokens"],
        record["compaction_tokens"],
    ) == (1_000_000, 400_000, 400_000)
    hub = [r for r in zephyros.records("hub") if r["action"] == "context.check"]
    assert hub
    assert hub[-1]["parameters"]["below_floor"] is False


def offline_environment(window: int, compaction: int) -> list[str]:
    """The variables a turn gets from the template, as `turn_spec` gives them."""
    return [
        "-e",
        "ZEPHYROS_HUB_URL=http://10.0.0.3:8080",
        "-e",
        "ZEPHYROS_MODEL=served-model",
        "-e",
        f"ZEPHYROS_TOKEN={TOKEN}",
        "-e",
        f"ZEPHYROS_CONTEXT_WINDOW={window}",
        "-e",
        f"ZEPHYROS_COMPACTION_TOKENS={compaction}",
        "-e",
        "HERMES_HOME=/opt/data",
    ]


def test_adr_0033_hermes_takes_the_window_and_compacts_where_the_hub_says(
    zephyros: Deployment,
) -> None:
    done = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "--tmpfs",
        "/opt/data:rw,size=64m,uid=10000,gid=10000",
        *offline_environment(1_000_000, 400_000),
        "--entrypoint",
        "sh",
        zephyros.instance_image(),
        "-c",
        'cp /opt/zephyros/seed.yaml "$HERMES_HOME/config.yaml" && cd /tmp && '
        'exec /opt/hermes/.venv/bin/python -I -c "$0"',
        HERMES_READS,
    )
    assert json.loads(done.stdout.strip().splitlines()[-1]) == [1_000_000, 400_000]


def test_adr_0033_opencode_takes_the_window_and_compacts_where_the_hub_says(
    zephyros: Deployment,
) -> None:
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
        *offline_environment(1_000_000, 400_000),
        OPENCODE_IMAGE,
        "debug",
        "config",
    )
    config = json.loads(done.stdout)
    model = config["provider"]["zephyros"]["models"]["served-model"]
    assert model["limit"] == {"context": 1_000_000, "input": 416_000, "output": 16_000}
