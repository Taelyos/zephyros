"""The instance image carries the test runner of the "tests" kit (ADR 0024, sealed level), and
the managed scope that leaves the agent every power inside its instance (ADR 0031), keeps the
reasoning of the model when the hub says so (ADR 0032) and compacts where the hub says (ADR
0033), and the entrypoint that starts a new session when the operator asks (ADR 0016)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _pins(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    return [" ".join(line.split()) for line in text.splitlines() if line and line[0] != "#"]


def test_the_instance_installs_the_same_pinned_wheels_as_the_kit() -> None:
    """A sealed instance runs the tests it writes with the runner that will judge them: the
    same versions and the same hashes, so neither file can move without the other."""
    instance = _pins(ROOT / "images" / "instance" / "requirements-tests.txt")
    kit = _pins(ROOT / "images" / "kit-tests" / "requirements.txt")
    assert instance == kit
    assert any(line.startswith("pytest==") for line in instance)


def test_the_instance_image_installs_them_by_hash_outside_the_venv_of_hermes() -> None:
    dockerfile = (ROOT / "images" / "instance" / "Dockerfile").read_text(encoding="utf-8")
    install = next(line for line in dockerfile.splitlines() if "pip install" in line)
    assert install.split()[1:4] == ["/usr/local/bin/python3", "-m", "pip"]
    flat = dockerfile.replace("\\\n", " ")
    assert "--require-hashes" in flat
    assert "--only-binary :all:" in flat
    assert "ENV PATH=/opt/zephyros/bin:" in dockerfile


# ----------------------------------------------------------------------------- the managed scope
MANAGED = ROOT / "images" / "instance" / "managed.yaml"
ENTRYPOINT = ROOT / "images" / "instance" / "entrypoint.sh"


def _section(name: str) -> list[str]:
    """The lines of one top-level section of the managed scope, comments left out."""
    lines = MANAGED.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{name}:") + 1
    found: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        if line.strip() and not line.lstrip().startswith("#"):
            found.append(line.strip())
    return found


def test_hermes_neither_asks_nor_refuses_in_a_turn() -> None:
    """ADR 0031: approvals off, in every context a turn can be in, with no deny rule, in the
    managed scope that the agent's own configuration cannot override. "off" is quoted: bare,
    YAML reads it as false."""
    assert _section("approvals") == [
        'mode: "off"',
        "single_query_mode: approve",
        "unattended_mode: approve",
        "cron_mode: approve",
        "deny: []",
    ]
    assert _section("security") == ["protected_instruction_files: false"]


def test_the_model_entry_keeps_the_reasoning_when_the_hub_says_so() -> None:
    assert "reasoning_echo: ${ZEPHYROS_REASONING_ECHO}" in _section("model")


HANDED = ("ZEPHYROS_REASONING_ECHO", "ZEPHYROS_CONTEXT_WINDOW", "ZEPHYROS_COMPACTION_TOKENS")


def _entrypoint(tmp_path: Path, environment: dict[str, str]) -> dict[str, str]:
    """What the entrypoint hands Hermes, with a recorder in the place of Hermes and of the
    helper of a new session, and a home in a temporary directory: each variable of HANDED, and
    `new_session` with the words of the helper when it ran."""
    if shutil.which("sh") is None:
        pytest.skip("not applicable here: no POSIX shell")
    tmp_path.mkdir(parents=True, exist_ok=True)
    recorder = tmp_path / "hermes"
    recorder.write_text(
        "#!/bin/sh\n"
        + "".join(f'printf "%s" "${{{name}-unset}}" > "{tmp_path}/{name}"\n' for name in HANDED),
        encoding="utf-8",
    )
    helper = tmp_path / "python"
    helper.write_text(f'#!/bin/sh\nprintf "%s " "$@" > "{tmp_path}/new_session"\n', "utf-8")
    for program in (recorder, helper):
        program.chmod(0o755)
    seed = tmp_path / "seed.yaml"
    seed.write_text("model: {}\n", encoding="utf-8")
    text = ENTRYPOINT.read_text(encoding="utf-8")
    replaced = {
        "/opt/hermes/.venv/bin/hermes": str(recorder),
        "/opt/hermes/.venv/bin/python": str(helper),
        "/opt/zephyros/seed.yaml": str(seed),
    }
    for path, local in replaced.items():
        assert text.count(path) == 1, path
        text = text.replace(path, local)
    script = tmp_path / "entrypoint.sh"
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    subprocess.run(  # noqa: S603  # reason: the repository's own script, with a recorder
        [str(script), "chat"],
        env={
            "HERMES_HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **environment,
        },
        check=True,
        timeout=30,
    )
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "model: {}\n"
    handed = {name: (tmp_path / name).read_text(encoding="utf-8") for name in HANDED}
    if (tmp_path / "new_session").exists():
        handed["new_session"] = (tmp_path / "new_session").read_text(encoding="utf-8").strip()
    return handed


def _expanded_echo(tmp_path: Path, environment: dict[str, str]) -> str:
    return _entrypoint(tmp_path, environment)["ZEPHYROS_REASONING_ECHO"]


@pytest.mark.parametrize(
    ("given", "expanded"),
    [("true", "true"), ("false", ""), (None, ""), ("1", ""), ("True", "")],
)
def test_the_entrypoint_gives_hermes_true_or_an_empty_text(
    tmp_path: Path, given: str | None, expanded: str
) -> None:
    """Hermes takes the truth of the text its managed scope expands: "false", or a variable it
    cannot expand (it keeps the placeholder), would read as true."""
    environment = {} if given is None else {"ZEPHYROS_REASONING_ECHO": given}
    assert _expanded_echo(tmp_path, environment) == expanded


# ----------------------------------------------------------------------------- context (ADR 0033)
def test_the_managed_scope_takes_the_window_and_the_compaction_from_the_turn() -> None:
    assert "context_length: ${ZEPHYROS_CONTEXT_WINDOW}" in _section("model")
    assert _section("compression") == [
        "threshold: 0.95",
        "threshold_tokens: ${ZEPHYROS_COMPACTION_TOKENS}",
    ]


def test_the_entrypoint_hands_the_context_of_the_hub_or_its_defaults(tmp_path: Path) -> None:
    given = {"ZEPHYROS_CONTEXT_WINDOW": "1000000", "ZEPHYROS_COMPACTION_TOKENS": "900000"}
    handed = _entrypoint(tmp_path / "given", given)
    assert (handed["ZEPHYROS_CONTEXT_WINDOW"], handed["ZEPHYROS_COMPACTION_TOKENS"]) == (
        "1000000",
        "900000",
    )
    for broken in ({}, {"ZEPHYROS_CONTEXT_WINDOW": "1M", "ZEPHYROS_COMPACTION_TOKENS": "-1"}):
        handed = _entrypoint(tmp_path / "broken", broken)
        assert (handed["ZEPHYROS_CONTEXT_WINDOW"], handed["ZEPHYROS_COMPACTION_TOKENS"]) == (
            "256000",
            "224000",
        )


# ----------------------------------------------------------------------------- session (ADR 0016)
def test_the_entrypoint_starts_a_new_session_only_when_asked(tmp_path: Path) -> None:
    for session, ran in (("new", True), ("continue", False), (None, False), ("NEW", False)):
        home = tmp_path / str(session)
        home.mkdir()
        handed = _entrypoint(home, {} if session is None else {"ZEPHYROS_SESSION": session})
        assert ("new_session" in handed) is ran, session
        if ran:
            assert handed["new_session"] == "-I /opt/zephyros/new_session.py"


def test_the_new_session_follows_the_name_the_engine_continues() -> None:
    """The helper creates the next session of the lineage of the name `--continue` gives."""
    from zephyros.adapters.hermes.engine import SESSION

    helper = (ROOT / "images" / "instance" / "new_session.py").read_text(encoding="utf-8")
    assert f'SESSION = "{SESSION}"' in helper
    dockerfile = (ROOT / "images" / "instance" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chmod=0644 new_session.py /opt/zephyros/new_session.py" in dockerfile
