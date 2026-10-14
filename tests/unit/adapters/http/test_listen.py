"""Where the operator channel and the main-agent endpoint listen (ATK-13, ADR 0025)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from tests.support.sockets import socket_path
from zephyros.adapters.http.listen import (
    ListenError,
    core_address,
    interface_addresses,
    listen_hosts,
)
from zephyros.adapters.http.server import (
    SocketPathError,
    bind_unix_socket,
    build_server,
    stop_all,
)

POOL = "10.200.0.0/16"


def addresses(*texts: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address(text) for text in texts]


def test_core_is_the_one_address_outside_the_pool_and_the_loopback() -> None:
    found = addresses("127.0.0.1", "10.200.3.3", "172.20.0.2", "10.200.7.3")
    assert listen_hosts("core", POOL, found) == ("172.20.0.2", "127.0.0.1")
    assert core_address(POOL, found) == "172.20.0.2"


def test_an_address_written_in_the_configuration_is_kept_as_it_is() -> None:
    assert listen_hosts("127.0.0.1", POOL, addresses("10.200.3.3")) == ("127.0.0.1",)


@pytest.mark.parametrize(
    "found",
    [
        ("127.0.0.1", "10.200.3.3"),  # only instance networks: nothing to listen on
        ("127.0.0.1", "172.20.0.2", "192.168.5.2"),  # two candidates: no guess
        (),
    ],
)
def test_core_without_exactly_one_address_off_the_pool_refuses(found: tuple[str, ...]) -> None:
    with pytest.raises(ListenError, match="exactly one address"):
        listen_hosts("core", POOL, addresses(*found))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="not applicable: Linux only")
def test_the_addresses_of_this_namespace_include_the_loopback() -> None:
    assert ipaddress.IPv4Address("127.0.0.1") in interface_addresses()


def test_core_is_refused_outside_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ListenError, match="Linux container"):
        interface_addresses()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="not applicable: 127.0.0.2")
def test_a_server_listens_on_each_of_its_addresses_and_nowhere_else() -> None:
    app = Starlette(routes=[Route("/", lambda _request: PlainTextResponse("here"))])
    port = _free_port()
    server = build_server(app, ("127.0.0.2", "127.0.0.1"), port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.02)
    try:
        for host in ("127.0.0.1", "127.0.0.2"):
            assert httpx.get(f"http://{host}:{port}/", timeout=5).text == "here"
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.3", port), timeout=2).close()
    finally:
        stop_all([server])
        thread.join(timeout=15)


def test_a_unix_socket_is_created_with_its_mode_and_replaces_a_stale_one(tmp_path: Path) -> None:
    path = Path(socket_path(tmp_path))
    first = bind_unix_socket(path, 0o600)
    first.close()
    second = bind_unix_socket(path, 0o600)
    second.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_unix_socket_never_replaces_a_file(tmp_path: Path) -> None:
    path = Path(socket_path(tmp_path))
    path.write_text("mine", encoding="utf-8")
    with pytest.raises(SocketPathError, match="not a socket"):
        bind_unix_socket(path, 0o600)
    assert path.read_text(encoding="utf-8") == "mine"
