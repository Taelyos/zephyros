"""OpenCode as the agent engine (ADR 0029): the command and the environment of a turn, and the
entrypoint of its image, which gives OpenCode the prompt on its standard input."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from zephyros.adapters.opencode.engine import OPENCODE_VERSION, OpenCodeEngine
from zephyros.core.ports.agent_engine import MAX_PROMPT, Side, TurnRequest

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "images" / "instance-opencode" / "entrypoint.sh"
BINARY = "/opt/opencode/bin/opencode"
HELPER = "python3 -I /opt/zephyros/session.py"
SESSION_HELPER = ROOT / "images" / "instance-opencode" / "session.py"
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
    assert dict(description.environment) == {**ENVIRONMENT, "ZEPHYROS_REASONING_ECHO": "false"}


def test_a_turn_says_whether_the_model_wants_its_reasoning_back() -> None:
    wanted = TurnRequest(
        "p_a", Side.CONTRACTOR, "http://10.200.0.3:8080", "served-model", "zph_c_t", "Go.", True
    )
    assert OpenCodeEngine().describe_turn(wanted).environment["ZEPHYROS_REASONING_ECHO"] == "true"


def test_the_engine_names_the_version_the_image_pins() -> None:
    dockerfile = (ROOT / "images" / "instance-opencode" / "Dockerfile").read_text(encoding="utf-8")
    assert OpenCodeEngine().engine_version == OPENCODE_VERSION == "1.19.0"
    assert f"-{OPENCODE_VERSION}.tgz" in dockerfile


def make_entrypoint(directory: Path) -> Path:
    """The entrypoint of the image, with a recorder in the place of OpenCode: it writes the
    words it was given, what it read on its standard input and the field of the reasoning it
    was given. In the place of session.py, a
    script prints the file `found` and exits with the code in the file `code`, when they are
    there."""
    recorder = directory / "opencode"
    recorder.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{directory}/argv"\ncat > "{directory}/stdin"\n'
        f'printf "%s" "${{ZEPHYROS_REASONING_FIELD-unset}}" > "{directory}/field"\n',
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    finder = directory / "session.sh"
    finder.write_text(
        f'[ -f "{directory}/found" ] && cat "{directory}/found"\n'
        f'[ -f "{directory}/code" ] && exit "$(cat "{directory}/code")"\nexit 0\n',
        encoding="utf-8",
    )
    script = directory / "entrypoint.sh"
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert text.count(BINARY) == 2
    assert text.count(HELPER) == 1
    text = text.replace(BINARY, str(recorder)).replace(HELPER, f"sh {finder}")
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def entrypoint(tmp_path: Path) -> Path:
    if shutil.which("sh") is None:
        pytest.skip("not applicable here: no POSIX shell")
    return make_entrypoint(tmp_path)


def run(
    entrypoint: Path, *words: str, extra: dict[str, str] | None = None
) -> tuple[list[str], str]:
    subprocess.run(  # noqa: S603  # reason: the repository's own script, with a recorder
        [str(entrypoint), *words],
        env={**ENVIRONMENT, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(extra or {})},
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


@pytest.mark.parametrize(
    ("echo", "field"),
    [("true", "reasoning_content"), ("false", ""), (None, ""), ("yes", ""), ("TRUE", "")],
)
def test_the_entrypoint_names_the_field_of_the_reasoning_only_when_the_hub_says_true(
    entrypoint: Path, echo: str | None, field: str
) -> None:
    """ADR 0032: the configuration reads the field from the entrypoint, which sets it from the
    hub's true or false; anything else, or nothing, keeps no reasoning."""
    extra = {} if echo is None else {"ZEPHYROS_REASONING_ECHO": echo}
    run(entrypoint, "turn", "Go.", extra=extra)
    assert (entrypoint.parent / "field").read_text(encoding="utf-8") == field


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


def test_a_turn_continues_the_session_of_the_instance_when_there_is_one(entrypoint: Path) -> None:
    (entrypoint.parent / "found").write_text("ses_theinstance\n", encoding="utf-8")
    argv, stdin = run(entrypoint, "turn", "-p evil")
    assert argv == ["run", "--session", "ses_theinstance"]
    assert stdin == "-p evil\n"


@pytest.mark.parametrize(("found", "code"), [("--help\n", "0"), ("", "0"), ("ses_abc\n", "1")])
def test_without_a_session_found_a_turn_falls_back_to_the_last_one_of_the_directory(
    entrypoint: Path, found: str, code: str
) -> None:
    (entrypoint.parent / "found").write_text(found, encoding="utf-8")
    (entrypoint.parent / "code").write_text(code, encoding="utf-8")
    argv, _ = run(entrypoint, "turn", "Build it.")
    assert argv == ["run", "--continue"]


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


# ----------------------------------------------------------------------------- session.py
def make_database(home: Path, rows: list[tuple[str, str, str | None, int, int | None]]) -> None:
    """The columns of OpenCode 1.19.0's session table that session.py reads: id, project,
    parent, last update, archived."""
    folder = home / ".local" / "share" / "opencode"
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / "opencode.db") as db:
        db.execute(
            "create table session (id text primary key, project_id text, parent_id text, "
            "time_updated integer, time_archived integer)"
        )
        db.executemany("insert into session values (?, ?, ?, ?, ?)", rows)
    db.close()


def find_session(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # reason: the repository's own script
        [sys.executable, "-I", str(SESSION_HELPER)],
        env={"HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def test_the_session_is_the_last_root_session_whatever_its_project(tmp_path: Path) -> None:
    """A clone into /work, or a history started again, names the directory after another
    project: the session of the instance is still the one found."""
    make_database(
        tmp_path,
        [
            ("ses_first", "global", None, 1, None),
            ("ses_kept", "1c120bba", None, 5, None),
            ("ses_subagent", "38064afc", "ses_kept", 9, None),  # a task of the agent
            ("ses_archived", "38064afc", None, 8, 8),
        ],
    )
    assert find_session(tmp_path).stdout == "ses_kept\n"


def test_no_session_is_found_before_the_first_turn(tmp_path: Path) -> None:
    done = find_session(tmp_path)
    assert (done.stdout, done.stderr) == ("", "")


def test_a_database_that_cannot_be_read_gives_no_session_and_says_why(tmp_path: Path) -> None:
    folder = tmp_path / ".local" / "share" / "opencode"
    folder.mkdir(parents=True)
    (folder / "opencode.db").write_bytes(b"not a database" * 100)
    done = find_session(tmp_path)
    assert done.stdout == ""
    assert "the session of the instance is unreadable" in done.stderr


def test_a_session_id_that_is_not_one_is_never_given(tmp_path: Path) -> None:
    make_database(tmp_path, [("--help", "global", None, 1, None)])
    assert find_session(tmp_path).stdout == ""
