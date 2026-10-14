"""The container filter program (ADR 0014): the one process that reaches the container runtime.

It listens on a Unix socket for the closed requests of the hub and turns each into the fixed
operations of its templates. The socket is created here, not by the server: the server library
would open it to everyone, and the filter opens it to its owner and group only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from zephyros.adapters.config_file import load_config
from zephyros.adapters.docker.api import DockerApi
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import FileJournal, Journal
from zephyros.adapters.filter.reaper import Reaper
from zephyros.adapters.filter.server import build_filter_app
from zephyros.adapters.http.server import HubServer
from zephyros.core.config import Config, ConfigError
from zephyros.core.containers import Policy

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
SOCKET_MODE = 0o660
JOURNAL_NAME = "filter-journal.jsonl"


class FilterStartError(Exception):
    """The filter cannot start safely."""


def bind_socket(path: Path) -> socket.socket:
    """Create the listening socket at `path`, readable and writable by owner and group only.

    A stale socket from an earlier run is replaced. Anything else at the path stops the start:
    the filter does not delete files that are not its own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_socket():
        path.unlink()
    elif path.exists() or path.is_symlink():
        raise FilterStartError(f"{path} exists and is not a socket")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        listener.bind(str(path))
    finally:
        os.umask(previous)
    os.chmod(path, SOCKET_MODE)
    listener.setblocking(False)
    return listener


def build_filter_server(executor: Executor, journal: Journal) -> HubServer:
    config = uvicorn.Config(
        build_filter_app(executor, journal),
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
        limit_concurrency=8,
    )
    return HubServer(config)


async def run(
    config: Config,
    *,
    docker: DockerApi | None = None,
    journal: Journal | None = None,
    stop: asyncio.Event | None = None,
    docker_socket: str = DEFAULT_DOCKER_SOCKET,
) -> None:
    """Serve the filter until a signal arrives, or until `stop` is set."""
    own_journal = journal is None
    journal = journal or FileJournal(Path(config.paths.data_dir) / JOURNAL_NAME)
    runtime = docker or DockerApi.over_socket(docker_socket)
    listener = bind_socket(Path(config.paths.filter_socket))
    executor = Executor(runtime, Policy.from_config(config), journal)
    server = build_filter_server(executor, journal)
    loop = asyncio.get_running_loop()
    # A turn must not outlive the hub past its limit: the reaper
    # runs here, in the process that outlives the hub.
    reaper_stop = asyncio.Event()
    reaper_task = loop.create_task(
        Reaper(
            executor,
            config.limits.turn_seconds,
            grace_seconds=3 * config.runtime.poll_seconds,
        ).run(reaper_stop)
    )
    waiter: asyncio.Task[None] | None = None
    if stop is None:
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(number, _ask_to_exit, server)
    else:
        waiter = loop.create_task(_exit_when_set(stop, server))
    try:
        await server.serve(sockets=[listener])
    finally:
        reaper_stop.set()
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
        if waiter is not None:
            waiter.cancel()
        listener.close()
        with contextlib.suppress(FileNotFoundError):
            Path(config.paths.filter_socket).unlink()
        runtime.close()
        if own_journal and isinstance(journal, FileJournal):
            journal.close()


def _ask_to_exit(server: HubServer) -> None:
    server.should_exit = True


async def _exit_when_set(stop: asyncio.Event, server: HubServer) -> None:
    await stop.wait()
    server.should_exit = True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zephyros-filter")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--docker-socket", default=DEFAULT_DOCKER_SOCKET)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"refused to start: {error}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(config, docker_socket=args.docker_socket))
    except FilterStartError as error:
        print(f"refused to start: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
