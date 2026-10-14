"""Running a Starlette application with uvicorn, the way the hub needs it.

Two settings matter for security. Proxy headers are off, so the peer address of a connection
is the TCP peer and never a header the caller wrote (EXG-60). The server and date headers are
off, so the listener says nothing about itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Iterator, Sequence
from pathlib import Path

import uvicorn
from starlette.applications import Starlette


class HubServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the program that runs several, and that
    may listen on several addresses."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        also: Sequence[str] = (),
        listener: socket.socket | None = None,
    ) -> None:
        super().__init__(config)
        self.also = tuple(also)
        """Addresses to listen on besides the configured host, on the same port."""
        self.listener = listener
        """A socket bound beforehand (a Unix socket) to serve on instead."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        if sockets is None and self.listener is not None:
            sockets = [self.listener]
        if sockets is None and self.also:
            sockets = [self.config.bind_socket()]
            for host in self.also:
                sockets.append(
                    uvicorn.Config(self.config.app, host=host, port=self.config.port).bind_socket()
                )
        await super().serve(sockets)


class SocketPathError(Exception):
    """Something other than a socket stands where a Unix socket is to be created."""


def bind_unix_socket(path: Path, mode: int) -> socket.socket:
    """Create a listening Unix socket at `path` with `mode`, never more open in between.

    A stale socket from an earlier run is replaced. Anything else at the path stops the start:
    nothing here deletes a file that is not a socket."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_socket():
        path.unlink()
    elif path.exists() or path.is_symlink():
        raise SocketPathError(f"{path} exists and is not a socket")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        listener.bind(str(path))
    finally:
        os.umask(previous)
    os.chmod(path, mode)
    listener.setblocking(False)
    return listener


def build_server(app: Starlette, host: str | Sequence[str], port: int) -> HubServer:
    """A server on `host`, or on each address of a sequence."""
    hosts = (host,) if isinstance(host, str) else tuple(host)
    config = uvicorn.Config(
        app,
        host=hosts[0],
        port=port,
        proxy_headers=False,
        forwarded_allow_ips=None,
        server_header=False,
        date_header=False,
        access_log=False,
        log_level="warning",
        lifespan="off",
        ws="none",
        http="h11",
        loop="asyncio",
    )
    return HubServer(config, also=hosts[1:])


async def serve_all(servers: list[HubServer]) -> None:
    """Run the servers together until each has been asked to exit."""
    await asyncio.gather(*(server.serve() for server in servers))


def serve_on(app: Starlette, listener: socket.socket) -> HubServer:
    """A server on a socket bound beforehand, such as one from bind_unix_socket."""
    server = build_server(app, "127.0.0.1", 0)
    server.listener = listener
    return server


def stop_all(servers: list[HubServer]) -> None:
    for server in servers:
        server.should_exit = True
