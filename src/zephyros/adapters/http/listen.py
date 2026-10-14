"""Where the operator channel and the main-agent endpoint listen (ATK-13, ADR 0025).

The hub container is attached to its core network, which carries the publication of its ports on
the loopback interface of the machine, and to the network of every instance. A listener on the
wildcard address would answer on the instance networks too, behind nothing but the guards. The
value "core" of `hub.operator_bind` and `hub.main_agent_bind` names instead the one address of
the hub outside the instance pool, and the loopback of its own container (for its health check
and for `scripts/deploy.sh call`). Every instance network lies inside the pool, so neither
listener is ever on one, whatever guard runs or does not.
"""

from __future__ import annotations

import fcntl
import ipaddress
import socket
import struct
import sys
from collections.abc import Iterable

from zephyros.core.schema import CORE_ADDRESS

LOOPBACK = "127.0.0.1"
_SIOCGIFADDR = 0x8915
"""The ioctl that reads the IPv4 address of an interface, on Linux."""


class ListenError(Exception):
    """No address to listen on that keeps off the instance networks: the hub does not start."""


def interface_addresses() -> list[ipaddress.IPv4Address]:
    """The IPv4 address of each interface of this network namespace (Linux)."""
    if not sys.platform.startswith("linux"):
        raise ListenError(f'"{CORE_ADDRESS}" is for a hub that runs in a Linux container')
    found: list[ipaddress.IPv4Address] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _index, name in socket.if_nameindex():
            request = struct.pack("256s", name.encode()[:15])
            try:
                answer = fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, request)
            except OSError:  # an interface without an IPv4 address
                continue
            found.append(ipaddress.IPv4Address(answer[20:24]))
    return found


def core_address(pool: str, addresses: Iterable[ipaddress.IPv4Address]) -> str:
    """The one address that is neither a loopback nor in the instance pool."""
    network = ipaddress.ip_network(pool)
    outside = sorted(
        {address for address in addresses if not address.is_loopback and address not in network}
    )
    if len(outside) != 1:
        named = ", ".join(str(address) for address in outside) or "none"
        raise ListenError(
            f'"{CORE_ADDRESS}" needs exactly one address of the hub outside the instance pool '
            f"{pool} and the loopback; found {named}"
        )
    return str(outside[0])


def listen_hosts(
    bind: str, pool: str, addresses: Iterable[ipaddress.IPv4Address] | None = None
) -> tuple[str, ...]:
    """The addresses a listener configured with `bind` listens on."""
    if bind != CORE_ADDRESS:
        return (bind,)
    found = interface_addresses() if addresses is None else addresses
    return (core_address(pool, found), LOOPBACK)
