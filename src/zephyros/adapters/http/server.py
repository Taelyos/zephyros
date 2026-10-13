"""Running a Starlette application with uvicorn, the way the hub needs it.

Two settings matter for security. Proxy headers are off, so the peer address of a connection
is the TCP peer and never a header the caller wrote (EXG-60). The server and date headers are
off, so the listener says nothing about itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import uvicorn
from starlette.applications import Starlette


class HubServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the program that runs several."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def build_server(app: Starlette, host: str, port: int) -> HubServer:
    config = uvicorn.Config(
        app,
        host=host,
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
    return HubServer(config)


async def serve_all(servers: list[HubServer]) -> None:
    """Run the servers together until each has been asked to exit."""
    await asyncio.gather(*(server.serve() for server in servers))


def stop_all(servers: list[HubServer]) -> None:
    for server in servers:
        server.should_exit = True
