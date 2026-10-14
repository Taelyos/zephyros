"""Unix socket paths for the tests: the operating systems limit them to about a hundred bytes."""

from __future__ import annotations

import tempfile
from pathlib import Path

LIMIT = 100


def socket_path(directory: Path, name: str = "c.sock") -> str:
    """`name` in `directory` when the path is short enough, else in a short new directory."""
    path = directory / name
    if len(str(path)) < LIMIT:
        return str(path)
    return str(Path(tempfile.mkdtemp(prefix="zs")) / name)
