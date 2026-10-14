"""The egress proxy of the hub (ADR 0024) on real loopback sockets.

The names resolve through a scripted resolver and every upstream connection lands on a local
echo server, whatever address the proxy asked for: the tests prove what the proxy decides,
relays and logs, without the Internet.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from tests.unit.core.hub_fixtures import Env, make_env
from zephyros.adapters.http.egress_proxy import EgressProxy
from zephyros.core.audit import Action, AuditRecord, Outcome, Reason
from zephyros.core.config import DEFAULT_ALLOWED_HOSTS
from zephyros.core.instance_egress import EgressLevel, InstanceEgressPolicy
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import instance_principal

NAMES = {
    "pypi.org": ["151.101.0.223", "151.101.64.223"],
    "mirror.example.org": ["151.101.0.223", "10.77.0.5"],  # one protected answer is enough
    "gone.example.org": [],
}
PAYLOAD = b"\x16\x03\x01 a client hello that is never read"


@dataclass
class World:
    env: Env
    token: str
    policy: InstanceEgressPolicy
    opened: list[tuple[str, int]] = field(default_factory=list)
    unreachable: bool = False

    def egress(self) -> list[AuditRecord]:
        return [r for r in self.env.records() if r.action is Action.INSTANCE_EGRESS]


def make_world(level: EgressLevel = EgressLevel.REGULATED, hosts: tuple[str, ...] = ()) -> World:
    env = make_env()
    principal = instance_principal(Side.CONTRACTOR, env.project_a, "live")
    token = env.tokens.issue_project_token(principal, "i_live", "127.0.0.1", 600).secret
    allowed = (*DEFAULT_ALLOWED_HOSTS, *hosts)
    return World(env, token, InstanceEgressPolicy(level, allowed))


def run(world: World, client: Callable[[int], Awaitable[bytes]]) -> bytes:
    async def scenario() -> bytes:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]

        async def resolve(host: str, _port: int) -> list[str]:
            return NAMES.get(host, ["203.0.113.9"])

        async def open_connection(
            address: str, port: int
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            world.opened.append((address, port))
            if world.unreachable:
                raise ConnectionRefusedError
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = EgressProxy(
            world.env.hub,
            world.env.audit,
            world.policy,
            resolve=resolve,
            open_connection=open_connection,
        )
        server = await proxy.start("127.0.0.1", 0)
        try:
            return await asyncio.wait_for(client(server.sockets[0].getsockname()[1]), 10)
        finally:
            server.close()
            upstream.close()

    return asyncio.run(scenario())


def basic(token: str) -> str:
    return "Basic " + base64.b64encode(f"instance:{token}".encode()).decode()


def ask(
    request_line: str, authorization: str | None, payload: bytes = b""
) -> Callable[[int], Awaitable[bytes]]:
    async def client(port: int) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        head = f"{request_line}\r\nHost: x\r\n"
        if authorization is not None:
            head += f"Proxy-Authorization: {authorization}\r\n"
        writer.write((head + "\r\n").encode())
        await writer.drain()
        answer = await reader.readuntil(b"\r\n\r\n")
        if answer.startswith(b"HTTP/1.1 200") and payload:
            writer.write(payload)
            await writer.drain()
            answer += await reader.readexactly(len(payload))
        writer.close()
        return answer

    return client


def parameters(record: AuditRecord) -> dict[str, object]:
    found: dict[str, object] = json.loads(record.parameters)
    return found


def test_an_allowed_registry_is_relayed_and_logged_under_the_instance() -> None:
    world = make_world()
    answer = run(world, ask("CONNECT pypi.org:443 HTTP/1.1", basic(world.token), PAYLOAD))
    assert answer.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert answer.endswith(PAYLOAD)  # the bytes went through, both ways
    assert world.opened == [("151.101.0.223", 443)]
    [record] = world.egress()
    project = world.env.project_a
    assert (record.stream, record.principal) == (project, f"contractor:{project}:live")
    assert record.outcome is Outcome.OK
    assert parameters(record) == {
        "host": "pypi.org",
        "port": 443,
        "source": "127.0.0.1",
        "level": "regulated",
        "side": "contractor",
    }
    # nothing that travelled in the tunnel is written down
    assert all("client hello" not in r.parameters for r in world.env.records())


@pytest.mark.parametrize(
    ("line", "host", "port", "why"),
    [
        ("CONNECT example.org:443 HTTP/1.1", "example.org", 443, "not_allowed"),
        ("CONNECT github.com:443 HTTP/1.1", "github.com", 443, "not_allowed"),
        ("CONNECT 151.101.0.223:443 HTTP/1.1", "151.101.0.223", 443, "not_allowed"),
        ("CONNECT pypi.org:22 HTTP/1.1", "pypi.org", 22, "port"),
        ("GET http://example.org/steal?x=1 HTTP/1.1", "example.org", 0, "not_connect"),
        ("GET http://[broken/ HTTP/1.1", "", 0, "not_connect"),
        ("CONNECT \x01evil:443 HTTP/1.1", "?", 443, "not_allowed"),
    ],
)
def test_any_other_destination_is_refused_before_the_network_and_logged(
    line: str, host: str, port: int, why: str
) -> None:
    world = make_world()
    answer = run(world, ask(line, basic(world.token)))
    assert answer.split(b"\r\n")[0] in (
        b"HTTP/1.1 403 Forbidden",
        b"HTTP/1.1 405 Method Not Allowed",
    )
    assert world.opened == []
    [record] = world.egress()
    assert (record.outcome, record.reason) == (Outcome.REFUSED, Reason.EGRESS)
    found = parameters(record)
    assert (found["host"], found["port"], found["why"]) == (host, port, why)
    assert "steal" not in record.parameters  # the host is logged, not the request


def test_an_allowed_name_that_resolves_into_a_protected_network_is_refused() -> None:
    world = make_world(hosts=("mirror.example.org",))
    answer = run(world, ask("CONNECT mirror.example.org:443 HTTP/1.1", basic(world.token)))
    assert answer.startswith(b"HTTP/1.1 403")
    assert world.opened == []
    assert parameters(world.egress()[0])["why"] == "protected"


def test_an_allowed_name_that_does_not_resolve_is_refused() -> None:
    world = make_world(hosts=("gone.example.org",))
    answer = run(world, ask("CONNECT gone.example.org:443 HTTP/1.1", basic(world.token)))
    assert answer.startswith(b"HTTP/1.1 403")
    assert parameters(world.egress()[0])["why"] == "no_address"


def test_an_allowed_host_that_cannot_be_reached_is_logged_as_an_error() -> None:
    world = make_world()
    world.unreachable = True
    answer = run(world, ask("CONNECT pypi.org:443 HTTP/1.1", basic(world.token)))
    assert answer.startswith(b"HTTP/1.1 502")
    [record] = world.egress()
    assert record.outcome is Outcome.ERROR
    assert parameters(record)["why"] == "unreachable"


@pytest.mark.parametrize("level", [EgressLevel.SEALED, EgressLevel.OPEN])
def test_outside_the_regulated_level_the_proxy_relays_nothing(level: EgressLevel) -> None:
    world = make_world(level)
    answer = run(world, ask("CONNECT pypi.org:443 HTTP/1.1", basic(world.token)))
    assert answer.startswith(b"HTTP/1.1 403")
    assert world.opened == []
    assert parameters(world.egress()[0])["why"] == "not_regulated"


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic !!!", "Bearer zph_c_x", "Basic " + base64.b64encode(b"instance:").decode()],
)
def test_without_a_valid_token_the_proxy_asks_for_one_and_opens_nothing(
    authorization: str | None,
) -> None:
    world = make_world()
    answer = run(world, ask("CONNECT pypi.org:443 HTTP/1.1", authorization))
    assert answer.startswith(b"HTTP/1.1 407")
    assert b'Proxy-Authenticate: Basic realm="zephyros"' in answer
    assert world.opened == []
    assert world.egress() == []
    assert [r for r in world.env.records() if r.action is Action.AUTHENTICATION]


def test_a_token_bound_to_another_address_is_refused() -> None:
    world = make_world()
    other = world.env.tokens.issue_project_token(
        instance_principal(Side.OWNER, world.env.project_a, "live"), "i_other", "10.200.0.2", 600
    ).secret
    answer = run(world, ask("CONNECT pypi.org:443 HTTP/1.1", basic(other)))
    assert answer.startswith(b"HTTP/1.1 407")
    assert world.opened == []
    refusals = [r for r in world.env.records() if r.action is Action.AUTHENTICATION]
    assert [r.reason for r in refusals] == [Reason.WRONG_SOURCE]
