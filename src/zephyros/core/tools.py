"""The tools of a project (ADR 0030): a directory of the machine, mounted read-only.

Some projects need heavy, fixed tooling that neither side may fetch or change: a simulator, a
headless engine, a test bench and its scripts. The operator names one directory of the machine
(`tools.source`); the container filter mounts it read-only at `TOOLS_PATH` in every turn, Owner
and Contractor alike, and in every sandbox of verification, so the gate judges with the tools
the agents had.

The hub fingerprints the directory when it starts, at each start of a turn and around each
verification, and writes the fingerprint to the log and to the evidence report, so that a verdict
can be replayed with the same tools. The tools are the operator's, and trusted: Zephyros keeps
everyone inside it from changing them; it does not vouch for what they do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

TOOLS_PATH = "/opt/project-tools"
"""Where the tools are, in every turn and every sandbox: one fixed path, so that a test, a script
or a note of the contract that names it holds on both sides and at the gate."""

TOOLS_BIN = TOOLS_PATH + "/bin"
"""The directory of the tools that the images put last on the PATH: a tool there runs by its
name, and never hides a program of the image."""

MAX_PROBLEMS = 20
"""The most problems a refusal names; it counts the others."""


@dataclass(frozen=True, slots=True)
class ToolsDigest:
    """What the hub measured of the tools: the fingerprint of the whole tree, and its size."""

    fingerprint: str
    """`sha256:` and 64 hexadecimal digits (docs/configuration.md, "The tools of a project")."""
    files: int
    bytes: int

    @property
    def short(self) -> str:
        """The fingerprint as the log and the report show it next to the size."""
        return self.fingerprint[: len("sha256:") + 16]


class ToolsError(Exception):
    """The directory cannot be mounted as the tools of a project: each problem names a path."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        shown = list(problems[:MAX_PROBLEMS])
        if len(problems) > MAX_PROBLEMS:
            shown.append(f"and {len(problems) - MAX_PROBLEMS} more")
        lines = "\n".join(f"  {problem}" for problem in shown)
        super().__init__(f"the tools directory cannot be mounted:\n{lines}")


class ToolsProbe(Protocol):
    """Measures the tools directory, each time it is asked."""

    def digest(self) -> ToolsDigest:
        """The fingerprint of the directory as it is now. Raises ToolsError when the directory
        is missing, or holds what the tools of a project may not hold."""
        ...
