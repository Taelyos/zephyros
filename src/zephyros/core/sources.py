"""The sources a project may come from (EXG-44; annex "Accepted repository sources").

Version 1 of the annex accepts the https URLs of repositories hosted on GitHub, and nothing
else. The check is on the exact host, the scheme and the shape of the path: no local path, no
private address, no credential, no query, no port ever travels as a project source — the hub
would otherwise act against the machine of the human.
"""

from __future__ import annotations

import re

PATTERN = re.compile(
    r"^https://github\.com/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/"  # the owner
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:\.git)?$"  # the repository
)

REFUSAL = (
    'the source is refused: annex "Accepted repository sources" accepts the https URLs of '
    "repositories hosted on GitHub only"
)


def accepted(url: str) -> bool:
    """Whether the annex accepts this URL as a project source."""
    return PATTERN.fullmatch(url) is not None
