"""The egress of the hub (EXG-48, ATK-83): the hub reaches the hosts the configuration names,
and no other. No telemetry, no hidden update check.

Every outbound destination is checked against the names the configuration carries: the model
provider, the notification receiver, and the accepted repository sources (annex v1: GitHub
over https). A redirect is a new destination: the hub does not follow one, and a redirect
pointing outside the configured hosts is refused and logged. A refusal never reaches the
network.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit


def host_of(url: str) -> str:
    """The host of an http(s) url, normalised; '' when the url names none."""
    parts = urlsplit(url)
    return normalise(parts.hostname or "")


def normalise(host: str) -> str:
    return host.strip().lower().rstrip(".")


GITHUB_HOST = "github.com"
"""The one host of the accepted repository sources, annex v1 (EXG-44)."""


@dataclass(frozen=True, slots=True)
class Egress:
    """What the hub attempted: the host, why, and where it was told to go."""

    host: str
    purpose: str
    detail: str = ""


class EgressRefusal(Exception):  # noqa: N818  # reason: a decision of the system, not a fault
    """An outgoing connection the configuration does not allow. It is logged, never sent."""

    def __init__(self, egress: Egress) -> None:
        self.egress = egress
        super().__init__(f"{egress.purpose} to {egress.host!r} is not configured")


class EgressPolicy:
    """The only hosts the hub may call."""

    def __init__(self, hosts: Iterable[str]) -> None:
        self._hosts = {normalise(host) for host in hosts if normalise(host)}

    def hosts(self) -> list[str]:
        return sorted(self._hosts)

    def allows(self, host: str) -> bool:
        return normalise(host) in self._hosts

    def allows_url(self, url: str) -> bool:
        return self.allows(host_of(url))

    def check(self, url: str, *, purpose: str) -> None:
        """Raise `EgressRefusal` unless the url names a configured host."""
        host = host_of(url)
        if not host or not self.allows(host):
            raise EgressRefusal(Egress(host or url, purpose, "not a configured host"))
