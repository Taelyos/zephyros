"""The sources a project may come from (EXG-44).

The hub accepts the https URLs of repositories hosted on GitHub, and nothing else. The check is
on the exact host, the scheme and the shape of the path: no local path, no private address, no
credential, no query, no port ever travels as a project source — the hub would otherwise act
against the machine of the human.
"""

from __future__ import annotations

import re

PATTERN = re.compile(
    r"^https://github\.com/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/"  # the owner
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:\.git)?$"  # the repository
)

REFUSAL = (
    "the source is refused: a project comes only from the https URL of a repository hosted on "
    "GitHub, never from a local path, another host or a private address"
)


def accepted(url: str) -> bool:
    """Whether the hub accepts this URL as a project source."""
    return PATTERN.fullmatch(url) is not None
