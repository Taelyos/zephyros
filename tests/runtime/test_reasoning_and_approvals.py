"""ADR 0031 and ADR 0032, on a whole Zephyros (scripts/deploy.sh), with no real model.

The provider behind the gateway is `demanding_provider.py`, run in the hub container: it answers
what the obedient stub answers, refuses a conversation in which an assistant message with tool
calls has no reasoning, as DeepSeek in thinking mode does, and answers once with two tool calls
and no reasoning. The model has neutral names on both sides of the gateway (`served-model`,
`thinking-model`), so nothing rests on a name holding "deepseek": the configuration says
`reasoning_echo = true`. The Owner runs Hermes, the Contractor OpenCode.

- ADR 0031: a Hermes turn runs `python3 -c`, a here-document and `execute_code`, which the
  pinned Hermes flags, without a refusal.
- ADR 0032: a Hermes turn and an OpenCode turn go to their end, no call is refused, and the
  provider gets back the reasoning it gave.
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
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

OWNER, CONTRACTOR = Side.OWNER, Side.CONTRACTOR
PORT = 9811
PROVIDER = (Path(__file__).parent / "demanding_provider.py").read_text(encoding="utf-8")
HUB_PYTHON = "/opt/zephyros/bin/python"

PYTHON_C = "python3 -c \"open('/work/from-c.txt', 'w').write('python -c ran')\""
HEREDOC = "python3 <<'PY'\nopen('/work/from-heredoc.txt', 'w').write('heredoc ran')\nPY"
CODE = "open('/work/from-code.txt', 'w').write('execute_code ran')"

FLAGGED = """
import json, sys
from tools.approval_detection import detect_dangerous_command
print(json.dumps([list(detect_dangerous_command(c)) for c in json.loads(sys.argv[1])]))
"""

ASK = """
import json, sys, urllib.request, urllib.error
url, key, body = sys.argv[1], sys.argv[2], sys.argv[3]
request = urllib.request.Request(url, data=body.encode() or None, headers={
    "authorization": "Bearer " + key, "content-type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=10) as answer:
        print(json.dumps([answer.status, json.load(answer)]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, json.load(error)]))
"""


@pytest.fixture(scope="module")
def key() -> str:
    return "sk-fake-" + secrets.token_hex(12)


@pytest.fixture(scope="module")
def zephyros(tmp_path_factory: pytest.TempPathFactory, key: str) -> Iterator[Deployment]:
    state = tmp_path_factory.mktemp("deployment")
    key_file = tmp_path_factory.mktemp("key") / "gateway.key"
    key_file.write_text(key + "\n", encoding="utf-8")
    key_file.chmod(0o644)  # a fake key, read by the hub as uid 10001
    deployment = Deployment(
        state,
        pool="10.237.0.0/16",
        extra_config=(
            "[gateway]\n"
            'provider = "openai-compatible"\n'
            f'upstream_url = "http://127.0.0.1:{PORT}/v1"\n'
            'credential_file = "/run/zephyros-credentials/gateway.key"\n'
            'model = "served-model"\n'
            'upstream_model = "thinking-model"\n'
            'models = ["thinking-model"]\n'
            "reasoning_echo = true\n"
            "timeout_seconds = 60\n"
        ),
        env_extra={
            "ZEPHYROS_CONTRACTOR_ENGINE": "opencode",
            "ZEPHYROS_GATEWAY_KEY_FILE": str(key_file),
        },
    )
    deployment.up()
    try:
        docker("exec", "-d", deployment.hub, HUB_PYTHON, "-c", PROVIDER, str(PORT), key)
        stats(deployment)  # it listens
        yield deployment
    finally:
        deployment.down()


def ask(zephyros: Deployment, key: str, path: str, body: object = None) -> tuple[int, Any]:
    """A request to the provider, from inside the hub, where it listens."""
    for _ in range(50):
        done = docker(
            "exec",
            zephyros.hub,
            HUB_PYTHON,
            "-c",
            ASK,
            f"http://127.0.0.1:{PORT}{path}",
            key,
            "" if body is None else json.dumps(body),
            check=False,
        )
        if done.returncode == 0:
            status, answer = json.loads(done.stdout)
            return int(status), answer
        time.sleep(0.2)
    raise AssertionError(f"the provider does not answer: {done.stderr[-500:]}")


def stats(zephyros: Deployment) -> dict[str, Any]:
    status, answer = ask(zephyros, "", "/stats")
    assert status == 200
    found: dict[str, Any] = answer
    return found


def lines(*instructions: str) -> str:
    return "\n".join(instructions)


def bash(command: str) -> str:
    return "TOOL bash " + json.dumps({"command": command, "description": "a step of the test"})


CALL = {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}


def conversation(reasoning: str | None) -> dict[str, Any]:
    assistant: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": [CALL]}
    if reasoning is not None:
        assistant["reasoning_content"] = reasoning
    return {
        "model": "thinking-model",
        "messages": [
            {"role": "user", "content": "go"},
            assistant,
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
    }


def test_adr_0032_the_provider_refuses_a_tool_call_that_comes_back_without_its_reasoning(
    zephyros: Deployment, key: str
) -> None:
    """The control: the provider behind the gateway is as demanding as DeepSeek."""
    for reasoning in (None, ""):
        status, answer = ask(zephyros, key, "/v1/chat/completions", conversation(reasoning))
        assert status == 400
        assert "reasoning_content" in answer["error"]["message"]
    status, _answer = ask(zephyros, key, "/v1/chat/completions", conversation(" "))
    assert status == 200


def test_adr_0031_the_pinned_hermes_flags_the_commands_of_the_test(zephyros: Deployment) -> None:
    """The control: without the managed scope, a one-shot turn would refuse these."""
    done = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        "-w",
        "/opt/hermes",
        zephyros.instance_image(),
        "-c",
        FLAGGED,
        json.dumps([PYTHON_C, HEREDOC]),
    )
    flags = json.loads(done.stdout)
    assert [flag[0] for flag in flags] == [True, True], flags


def test_adr_0031_0032_a_hermes_turn_runs_its_scripts_and_gives_the_reasoning_back(
    zephyros: Deployment,
) -> None:
    project = zephyros.project()
    before = stats(zephyros)
    prompt = lines(
        f"RUN {PYTHON_C}",
        "TOOL terminal " + json.dumps({"command": HEREDOC}),
        f"CODE {CODE}",
        "RUN echo control > /work/control-hermes",
    )
    assert zephyros.turn(project, OWNER, prompt) == "completed"
    assert zephyros.work_file(project, OWNER, "from-c.txt") == "python -c ran"
    assert zephyros.work_file(project, OWNER, "from-heredoc.txt") == "heredoc ran"
    assert zephyros.work_file(project, OWNER, "from-code.txt") == "execute_code ran"
    assert zephyros.work_file(project, OWNER, "control-hermes") == "control\n"
    results = zephyros.tool_results(project, OWNER)
    assert not [r for r in results if "BLOCKED" in r], results
    after = stats(zephyros)
    assert after["refused"] == before["refused"]
    assert after["without_reasoning"] > before["without_reasoning"]
    given = set(after["given"]) - set(before["given"])
    assert given & set(after["back"]), after  # Hermes kept the reasoning it was given
    errors = [
        r
        for r in zephyros.records(project)
        if r["action"] == "gateway.call" and r["outcome"] != "ok"
    ]
    assert errors == []


def test_adr_0032_an_opencode_turn_goes_to_its_end_and_gives_the_reasoning_back(
    zephyros: Deployment,
) -> None:
    project = zephyros.project()
    before = stats(zephyros)
    prompt = lines(
        bash("echo one > /work/oc-one"),
        bash("echo two > /work/oc-two"),
        bash("echo three > /work/oc-three"),
    )
    assert zephyros.turn(project, CONTRACTOR, prompt) == "completed"
    for name, text in (("oc-one", "one"), ("oc-two", "two"), ("oc-three", "three")):
        assert zephyros.work_file(project, CONTRACTOR, name) == text + "\n"
    after = stats(zephyros)
    assert after["refused"] == before["refused"]
    assert after["without_reasoning"] > before["without_reasoning"]
    given = set(after["given"]) - set(before["given"])
    assert given & set(after["back"]), after  # OpenCode kept the reasoning it was given
    errors = [
        r
        for r in zephyros.records(project)
        if r["action"] == "gateway.call" and r["outcome"] != "ok"
    ]
    assert errors == []
