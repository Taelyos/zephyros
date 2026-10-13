"""With real sockets: the peer address is the TCP peer, whatever headers say (EXG-60, ATK-85)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest

from tests.unit.core.hub_fixtures import Env, make_env
from zephyros.adapters.http.agent import build_agent_app
from zephyros.adapters.http.server import build_server, stop_all
from zephyros.core.audit import Reason
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import instance_principal


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def served() -> Iterator[tuple[Env, str, str]]:
    """A hub that serves its agent endpoint on loopback, and a token bound to 127.0.0.1 and one
    bound to an address no connection will have."""
    env = make_env()
    principal = instance_principal(Side.CONTRACTOR, env.project_a, "live")
    here = env.tokens.issue_project_token(principal, "i_live", "127.0.0.1", 600)
    elsewhere = env.tokens.issue_project_token(
        instance_principal(Side.OWNER, env.project_a, "live"), "i_other", "10.200.0.2", 600
    )
    port = free_port()
    server = build_server(build_agent_app(env.hub, model_name="m"), "127.0.0.1", port)
    thread = threading.Thread(target=lambda: __import__("asyncio").run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.02)
    try:
        yield env, f"http://127.0.0.1:{port}", here.secret + "|" + elsewhere.secret
    finally:
        stop_all([server])
        thread.join(timeout=15)


def test_the_peer_is_the_tcp_peer_and_forwarding_headers_change_nothing(
    served: tuple[Env, str, str],
) -> None:
    env, url, secrets = served
    mine, theirs = secrets.split("|")
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    ok = httpx.post(f"{url}/mcp", json=body, headers={"Authorization": f"Bearer {mine}"})
    assert ok.status_code == 200
    # A token bound to another address, with every header that claims that address.
    forged = httpx.post(
        f"{url}/mcp",
        json=body,
        headers={
            "Authorization": f"Bearer {theirs}",
            "X-Forwarded-For": "10.200.0.2",
            "X-Real-IP": "10.200.0.2",
            "Forwarded": "for=10.200.0.2",
            "X-Forwarded-Host": "10.200.0.2",
        },
    )
    assert forged.status_code == 401
    record = env.last(env.project_a)
    assert record.reason is Reason.WRONG_SOURCE
    assert '"source":"127.0.0.1"' in record.parameters


def test_the_listener_says_nothing_about_itself(served: tuple[Env, str, str]) -> None:
    _env, url, _secrets = served
    response = httpx.get(f"{url}/v1/models")
    assert "server" not in response.headers
    assert "date" not in response.headers
