"""Version numbers of the public interfaces: three numbers, as in semantic versioning 2.0.0."""

from __future__ import annotations

import re

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def parse_version(text: str) -> tuple[int, int, int]:
    """Split "MAJOR.MINOR.PATCH" into three integers. Anything else raises ValueError."""
    match = _VERSION.fullmatch(text)
    if match is None:
        raise ValueError(f"not a version number: {text!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)
