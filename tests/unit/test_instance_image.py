"""The instance image carries the test runner of the "tests" kit (ADR 0024, sealed level), and
the managed scope that leaves the agent every power inside its instance (ADR 0031) and keeps the
reasoning of the model when the hub says so (ADR 0032)."""

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


def _expanded_echo(tmp_path: Path, environment: dict[str, str]) -> str:
    """What the entrypoint hands Hermes in ZEPHYROS_REASONING_ECHO, with a recorder in the
    place of Hermes and a home in a temporary directory."""
    if shutil.which("sh") is None:
        pytest.skip("not applicable here: no POSIX shell")
    recorder = tmp_path / "hermes"
    recorder.write_text(
        f'#!/bin/sh\nprintf "%s" "${{ZEPHYROS_REASONING_ECHO-unset}}" > "{tmp_path}/echo"\n',
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    seed = tmp_path / "seed.yaml"
    seed.write_text("model: {}\n", encoding="utf-8")
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert text.count("/opt/hermes/.venv/bin/hermes") == 1
    assert text.count("/opt/zephyros/seed.yaml") == 1
    script = tmp_path / "entrypoint.sh"
    script.write_text(
        text.replace("/opt/hermes/.venv/bin/hermes", str(recorder)).replace(
            "/opt/zephyros/seed.yaml", str(seed)
        ),
        encoding="utf-8",
    )
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
    return (tmp_path / "echo").read_text(encoding="utf-8")


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
