"""Unix socket paths for the tests: the operating systems limit them to about a hundred bytes."""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path

LIMIT = 100


def socket_path(directory: Path, name: str = "c.sock") -> str:
    """`name` in `directory` when the path is short enough, else in a short new directory."""
    path = directory / name
    if len(str(path)) < LIMIT:
        return str(path)
    return str(Path(tempfile.mkdtemp(prefix="zs")) / name)


_GIVEN: set[int] = set()


def free_port() -> int:
    """A TCP port of the loopback that is free now and was not given before in this run: the
    probe is closed before the port is used, so the system may hand the same port out again,
    and a configuration that draws several of them would get one twice."""
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port not in _GIVEN:
            _GIVEN.add(port)
            return port
