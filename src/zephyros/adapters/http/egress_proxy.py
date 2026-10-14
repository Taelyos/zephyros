"""The egress proxy of the hub (ADR 0024): the one way out of a `regulated` instance.

The guard of a regulated instance lets through the agent endpoint and this listener, nothing
else. A turn reaches it through `HTTPS_PROXY`, which carries its project token. Each connection
is one `CONNECT host:443`: the proxy authenticates the token as the agent endpoint does (bound
to the address of the guard), decides from the allow-list, resolves the name itself, refuses a
name that resolves into a protected network, and then relays bytes it never reads. Every
destination asked for is logged once, under the instance, as `instance.egress`: allowed (`ok`),
refused, or allowed but not reached (`error`). The log holds the host, the port and why; never
anything that travelled in the tunnel.

A plain proxied request (`GET http://...`) is refused and logged with its host: the proxy opens
tunnels to https registries and serves nothing itself.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import socket
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit

from zephyros.core.audit import HUB_STREAM, Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.config import Config
from zephyros.core.hub import Caller, Hub
from zephyros.core.instance_egress import (
    MAX_HOST,
    Destination,
    EgressLevel,
    InstanceEgressPolicy,
    Refused,
    parse_connect,
)
from zephyros.core.tokens import Endpoint

MAX_HEAD: Final = 8192
HEAD_SECONDS: Final = 10.0
CONNECT_SECONDS: Final = 10.0
IDLE_SECONDS: Final = 300.0
CHUNK: Final = 65536

Resolve = Callable[[str, int], Awaitable[list[str]]]
Open = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

_STATUS: Final = {
    200: "Connection established",
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    407: "Proxy Authentication Required",
    502: "Bad Gateway",
}


async def resolve_ipv4(host: str, port: int) -> list[str]:
    """Every IPv4 address of a name, from the resolver of the hub. Instances are IPv4 only."""
    loop = asyncio.get_running_loop()
    try:
        found = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({str(item[4][0]) for item in found})


async def open_tcp(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address, port)


def _token(header: str) -> str | None:
    """The password of `Proxy-Authorization: Basic base64(user:password)`: the project token."""
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "basic" or not value.strip():
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _user, colon, password = decoded.partition(":")
    return password if colon and password else None


def _shown(text: str) -> str:
    """A host as the instance wrote it, bounded and printable, for the log."""
    bounded = text[:MAX_HOST]
    return bounded if bounded.isascii() and bounded.isprintable() else "?"


def _host_of(request_line: str) -> str:
    """The host a request that is not a CONNECT names (`GET http://host/...`), or ''."""
    parts = request_line.split(" ")
    if len(parts) != 3:
        return ""
    try:
        return urlsplit(parts[1]).hostname or ""
    except ValueError:
        return ""


class EgressProxy:
    def __init__(
        self,
        hub: Hub,
        audit: AuditLog,
        policy: InstanceEgressPolicy,
        *,
        resolve: Resolve = resolve_ipv4,
        open_connection: Open = open_tcp,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self._hub = hub
        self._audit = audit
        self._policy = policy
        self._resolve = resolve
        self._open = open_connection
        self._idle = idle_seconds

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEAD)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_SECONDS)
        try:
            lines = head.decode("ascii").split("\r\n")
        except UnicodeDecodeError:
            await self._answer(writer, 400)
            return
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon:
                headers[name.strip().lower()] = value.strip()
        peer = writer.get_extra_info("peername")
        source = str(peer[0]) if isinstance(peer, tuple) and peer else None
        caller = self._hub.authenticate(
            _token(headers.get("proxy-authorization", "")),
            peer_address=source,
            endpoint=Endpoint.AGENT,
        )
        if caller is None:
            await self._answer(writer, 407, 'Proxy-Authenticate: Basic realm="zephyros"\r\n')
            return
        destination = parse_connect(lines[0])
        if destination is None:
            self._log(caller, Destination(_host_of(lines[0]), 0), Refused.NOT_CONNECT)
            await self._answer(writer, 405)
            return
        refused = self._policy.decide(destination)
        addresses: list[str] = []
        if refused is None:
            addresses = await self._resolve(destination.host, destination.port)
            refused = self._policy.check_addresses(addresses)
        if refused is not None:
            self._log(caller, destination, refused)
            await self._answer(writer, 403)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self._open(addresses[0], destination.port), CONNECT_SECONDS
            )
        except (OSError, TimeoutError):
            self._log(caller, destination, None, reached=False)
            await self._answer(writer, 502)
            return
        self._log(caller, destination, None)
        await self._answer(writer, 200)
        try:
            await self._relay(reader, writer, upstream_reader, upstream_writer)
        finally:
            upstream_writer.close()
            with contextlib.suppress(OSError):
                await upstream_writer.wait_closed()

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        """Copy both ways until one side closes or both stay idle too long."""

        async def pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
            while chunk := await asyncio.wait_for(source.read(CHUNK), self._idle):
                target.write(chunk)
                await target.drain()

        tasks = [
            asyncio.ensure_future(pipe(reader, upstream_writer)),
            asyncio.ensure_future(pipe(upstream_reader, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _answer(self, writer: asyncio.StreamWriter, status: int, extra: str = "") -> None:
        writer.write(f"HTTP/1.1 {status} {_STATUS[status]}\r\n{extra}\r\n".encode())
        await writer.drain()

    def _log(
        self,
        caller: Caller,
        destination: Destination,
        refused: Refused | None,
        *,
        reached: bool = True,
    ) -> None:
        if refused is not None:
            outcome, reason = Outcome.REFUSED, Reason.EGRESS
        else:
            outcome, reason = (Outcome.OK if reached else Outcome.ERROR), Reason.NONE
        parameters: dict[str, object] = {
            "host": _shown(destination.host),
            "port": destination.port,
            "source": caller.source,
            "level": self._policy.level.value,
        }
        if caller.principal.kind.value in ("owner", "contractor"):
            parameters["side"] = caller.principal.kind.value
        if refused is not None:
            parameters["why"] = refused.value
        elif not reached:
            parameters["why"] = "unreachable"
        stream = caller.principal.project_id or HUB_STREAM
        self._audit.append(
            AuditEvent(
                stream, caller.principal.id, Action.INSTANCE_EGRESS, outcome, reason, parameters
            )
        )


async def start_egress_proxy(
    config: Config, hub: Hub, audit: AuditLog, host: str
) -> asyncio.Server | None:
    """The proxy of a regulated deployment, listening beside the agent endpoint; none at the
    other levels, where no guard lets an instance reach it."""
    if config.network.egress != EgressLevel.REGULATED:
        return None
    policy = InstanceEgressPolicy(config.network.egress, config.network.allowed_hosts)
    return await EgressProxy(hub, audit, policy).start(host, config.hub.egress_port)
