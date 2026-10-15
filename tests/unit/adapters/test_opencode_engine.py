"""OpenCode as the agent engine (ADR 0029): the command and the environment of a turn, and the
entrypoint of its image, which gives OpenCode the prompt on its standard input."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from zephyros.adapters.opencode.engine import OPENCODE_VERSION, OpenCodeEngine
from zephyros.core.ports.agent_engine import MAX_PROMPT, Side, TurnRequest

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "images" / "instance-opencode" / "entrypoint.sh"
BINARY = "/opt/opencode/bin/opencode"
ENVIRONMENT = {
    "ZEPHYROS_HUB_URL": "http://10.200.0.3:8080",
    "ZEPHYROS_MODEL": "m",
    "ZEPHYROS_TOKEN": "zph_c_t",
}


def request(prompt: str) -> TurnRequest:
    return TurnRequest("p_a", Side.CONTRACTOR, "http://10.200.0.3:8080", "m", "zph_c_t", prompt)


def test_a_turn_hands_the_prompt_to_the_entrypoint_as_one_word() -> None:
    description = OpenCodeEngine().describe_turn(request("Build it."))
    assert description.command == ("turn", "Build it.")
    assert dict(description.environment) == ENVIRONMENT


def test_the_engine_names_the_version_the_image_pins() -> None:
    dockerfile = (ROOT / "images" / "instance-opencode" / "Dockerfile").read_text(encoding="utf-8")
    assert OpenCodeEngine().engine_version == OPENCODE_VERSION == "1.19.0"
    assert f"-{OPENCODE_VERSION}.tgz" in dockerfile


def make_entrypoint(directory: Path) -> Path:
    """The entrypoint of the image, with a recorder in the place of OpenCode: it writes the
    words it was given and what it read on its standard input."""
    recorder = directory / "opencode"
    recorder.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{directory}/argv"\ncat > "{directory}/stdin"\n',
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    script = directory / "entrypoint.sh"
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert text.count(BINARY) == 2
    script.write_text(text.replace(BINARY, str(recorder)), encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def entrypoint(tmp_path: Path) -> Path:
    if shutil.which("sh") is None:
        pytest.skip("not applicable here: no POSIX shell")
    return make_entrypoint(tmp_path)


def run(entrypoint: Path, *words: str) -> tuple[list[str], str]:
    subprocess.run(  # noqa: S603  # reason: the repository's own script, with a recorder
        [str(entrypoint), *words],
        env={**ENVIRONMENT, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=30,
    )
    argv = (entrypoint.parent / "argv").read_text(encoding="utf-8").splitlines()
    return argv, (entrypoint.parent / "stdin").read_bytes().decode()  # no newline translation


@pytest.mark.parametrize(
    "prompt",
    [
        "Build it.",
        "-p evil",
        "--help",
        "--",
        "PROMPT",
        "line one\nPROMPT\nline three",
        "\r",
        "carriage\r\nreturns\r",
        "$(touch /tmp/zephyros-pwned) `id` ${HOME} $ZEPHYROS_TOKEN",
        'a "quoted" word, a \\ backslash and a \\n',
        'TOOL zephyros_contract_read {"project_id": "p_a"}',
    ],
)
def test_the_prompt_reaches_opencode_on_its_input_exactly_and_never_as_an_option(
    entrypoint: Path, prompt: str
) -> None:
    argv, stdin = run(entrypoint, *OpenCodeEngine().describe_turn(request(prompt)).command)
    assert argv == ["run", "--continue"]
    assert stdin == prompt + "\n"


@pytest.fixture(scope="module")
def shared_entrypoint(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("sh") is None:
        pytest.skip("not applicable here: no POSIX shell")
    return make_entrypoint(tmp_path_factory.mktemp("entrypoint"))


@settings(max_examples=60, deadline=None)
@given(
    st.text(
        st.characters(codec="utf-8", exclude_characters="\x00"),
        min_size=1,
        max_size=300,
    )
)
def test_any_prompt_reaches_opencode_unchanged(shared_entrypoint: Path, prompt: str) -> None:
    argv, stdin = run(shared_entrypoint, "turn", prompt)
    assert argv == ["run", "--continue"]
    assert stdin == prompt + "\n"


def test_another_command_line_goes_to_opencode_as_it_is(entrypoint: Path) -> None:
    argv, stdin = run(entrypoint, "debug", "config")
    assert argv == ["debug", "config"]
    assert stdin == ""


def test_a_turn_without_its_environment_does_not_start(entrypoint: Path) -> None:
    done = subprocess.run(  # noqa: S603  # reason: the repository's own script, with a recorder
        [str(entrypoint), "turn", "Build it."],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert done.returncode != 0
    assert not (entrypoint.parent / "argv").exists()


def test_the_longest_prompt_fits_on_the_command_line_of_the_entrypoint(entrypoint: Path) -> None:
    prompt = json.dumps("x" * (MAX_PROMPT - 2))
    assert len(prompt) == MAX_PROMPT
    _, stdin = run(entrypoint, "turn", prompt)
    assert stdin == prompt + "\n"
