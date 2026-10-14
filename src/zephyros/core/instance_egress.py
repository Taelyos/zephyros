"""The egress of an instance (ADR 0024): what it reaches besides the hub, at each level.

A `regulated` instance reaches the hub, and the allowed hosts through the egress proxy of the
hub. The proxy reads one `CONNECT host:443` per connection, decides here, and relays the bytes
of an allowed tunnel without reading them. A destination is allowed by its name, then refused
when the name resolves into a protected network, so that a poisoned or changed name cannot
open a way into the user's networks. A `sealed` instance reaches the hub alone; an `open` one
reaches every public destination directly, and the proxy is not on its path.

What this module decides never reaches the network: the proxy asks first, then connects.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from zephyros.core.config import HOST_NAME, PROTECTED_V4

TUNNEL_PORT = 443
"""The one port a tunnel opens: the allowed hosts are https registries."""

MAX_HOST = 253

PROTECTED_V6: tuple[str, ...] = (
    "::/128",
    "::1/128",
    "::ffff:0:0/96",
    "fc00::/7",
    "fe80::/10",
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "2002::/16",
    "2001::/32",
)
"""The IPv6 networks of annex "Protected networks", as the guard loads them
(images/guard/guard.sh)."""

_PROTECTED = tuple(ipaddress.ip_network(item) for item in PROTECTED_V4 + PROTECTED_V6)
_HOST = re.compile(HOST_NAME)
_TARGET = re.compile(r"^([^\s:/@\[\]]{1,253}):([0-9]{1,5})$")


class EgressLevel(StrEnum):
    REGULATED = "regulated"
    SEALED = "sealed"
    OPEN = "open"


class Refused(StrEnum):
    """Why the proxy refused a destination, as a stable code in the log."""

    NOT_CONNECT = "not_connect"
    """The request is not a CONNECT to host:port."""
    NOT_REGULATED = "not_regulated"
    """The level is not `regulated`: the proxy serves no instance."""
    PORT = "port"
    """A port other than 443."""
    NOT_ALLOWED = "not_allowed"
    """A host the allow-list does not name, or an address instead of a name."""
    NO_ADDRESS = "no_address"
    """The name does not resolve."""
    PROTECTED = "protected"
    """The name resolves into a protected network."""


@dataclass(frozen=True, slots=True)
class Destination:
    """What an instance asked for: a host as it wrote it (bounded), and a port."""

    host: str
    port: int


def is_protected(address: str) -> bool:
    """Whether an address is in a protected network; an unreadable one counts as protected."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return any(parsed in network for network in _PROTECTED)


def parse_connect(request_line: str) -> Destination | None:
    """The destination of `CONNECT host:port HTTP/1.x`, or None for anything else."""
    parts = request_line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        return None
    found = _TARGET.fullmatch(parts[1])
    if found is None:
        return None
    port = int(found.group(2))
    if not 0 < port < 65536:
        return None
    return Destination(found.group(1), port)


class InstanceEgressPolicy:
    """The level and the allow-list of the configuration, and the decisions they give."""

    def __init__(self, level: EgressLevel | str, hosts: Iterable[str]) -> None:
        self.level = EgressLevel(level)
        self._hosts = frozenset(host for host in hosts if _HOST.fullmatch(host))

    def hosts(self) -> list[str]:
        return sorted(self._hosts)

    def decide(self, destination: Destination) -> Refused | None:
        """None when the proxy may resolve the destination; otherwise why not.

        The host is compared as written: the allow-list is lower case and holds names only,
        so `PyPI.org`, `pypi.org.` or an address never match one of its entries."""
        if self.level is not EgressLevel.REGULATED:
            return Refused.NOT_REGULATED
        if destination.port != TUNNEL_PORT:
            return Refused.PORT
        if destination.host not in self._hosts:
            return Refused.NOT_ALLOWED
        return None

    @staticmethod
    def check_addresses(addresses: Iterable[str]) -> Refused | None:
        """None when every address a name resolved to is public; otherwise why not. One
        protected address refuses the whole name: the proxy never picks among them."""
        found = list(addresses)
        if not found:
            return Refused.NO_ADDRESS
        if any(is_protected(address) for address in found):
            return Refused.PROTECTED
        return None
