"""ADR 0030: the bin directory of the tools is the last one of the PATH in the three images where
the tools are mounted, so that a tool runs by its name on both sides and at the gate, and never
hides a program of the image (python, pytest, git)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from zephyros.core.tools import TOOLS_BIN

ROOT = Path(__file__).resolve().parents[2]
PATH = re.compile(r"""(?:^ENV|\\\n)\s+(?:[A-Z_]+=\S+\s+)*PATH=("?)([^"\s]+)\1""", re.MULTILINE)


@pytest.mark.parametrize("image", ["instance", "instance-opencode", "kit-tests"])
def test_the_tools_come_last_on_the_path_of_every_image(image: str) -> None:
    dockerfile = (ROOT / "images" / image / "Dockerfile").read_text(encoding="utf-8")
    paths = [found.group(2) for found in PATH.finditer(dockerfile)]
    assert len(paths) == 1, paths
    entries = paths[0].split(":")
    assert entries[-1] == TOOLS_BIN
    assert entries.count(TOOLS_BIN) == 1
    assert entries[0] != TOOLS_BIN


PROFILE = ROOT / "images" / "instance" / "project-tools.sh"


@pytest.mark.parametrize("image", ["instance", "instance-opencode"])
def test_a_login_shell_of_a_turn_finds_the_tools_too(image: str) -> None:
    """The terminal of Hermes runs its commands in a login shell, whose PATH /etc/profile sets
    again: a script of /etc/profile.d puts the tools back, last."""
    folder = ROOT / "images" / image
    assert (folder / "project-tools.sh").read_bytes() == PROFILE.read_bytes()
    dockerfile = (folder / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "COPY --chmod=0644 project-tools.sh /etc/profile.d/zephyros-project-tools.sh" in dockerfile
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("/usr/local/bin:/usr/bin:/bin", f"/usr/local/bin:/usr/bin:/bin:{TOOLS_BIN}"),
        (f"/usr/bin:{TOOLS_BIN}", f"/usr/bin:{TOOLS_BIN}"),  # once, however often it is read
    ],
)
def test_the_profile_adds_the_tools_last_and_once(before: str, after: str) -> None:
    shell = shutil.which("sh")
    assert shell is not None
    done = subprocess.run(  # noqa: S603  # reason: the repository's own profile script
        [shell, "-c", '. "$1"; . "$1"; printf %s "$PATH"', "sh", str(PROFILE)],
        env={"PATH": before},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == after
