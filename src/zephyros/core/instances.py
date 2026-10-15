"""Control of the instances, as the hub core sees it.

An instance is the network guard of one side of a project plus its two volumes, and each agent
container is one turn of it (ADR 0016). The core asks for an instance to be started with a
first prompt, to be stopped, or to be described; an adapter does it through the container
filter.

Starting and stopping take a while (a guard has to become healthy), so they are requests: the
call returns at once with the state it put the instance in, and a worker does the rest.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from zephyros.core.ports.agent_engine import Side

MAX_PROMPT = 4000


class InstanceState(StrEnum):
    ABSENT = "absent"
    STARTING = "starting"
    """The instance is being brought up, or a turn is being started."""
    IDLE = "idle"
    """The guard is healthy and no turn is running."""
    TURN = "turn"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    project_id: str
    side: Side
    state: InstanceState
    address: str | None = None
    turn_started: str | None = None
    last_turn: str | None = None
    """How the latest turn or start ended: completed, failed, timeout, silent, stopped, ..."""


@dataclass(frozen=True, slots=True)
class InstanceRecord:
    """What the hub keeps of an instance across restarts. Never a secret."""

    project_id: str
    side: Side
    instance_id: str
    subnet: str
    created_at: datetime


class InstanceStore(Protocol):
    def get(self, project_id: str, side: Side) -> InstanceRecord | None: ...

    def put(self, record: InstanceRecord) -> None: ...

    def remove(self, project_id: str, side: Side) -> None: ...

    def all(self) -> list[InstanceRecord]: ...


class InstanceControl(Protocol):
    def start(
        self, project_id: str, side: Side, prompt: str, *, new_session: bool = False
    ) -> InstanceInfo:
        """Make sure the instance exists and start a turn with `prompt`, in a new session of
        the instance when `new_session` (ADR 0016)."""
        ...

    def stop(self, project_id: str, side: Side) -> InstanceInfo:
        """End the running turn if any, and remove the instance and its tokens."""
        ...

    def status(self, project_id: str, side: Side) -> InstanceInfo: ...


class NoFreeSubnetError(Exception):
    """Every subnet of the pool is taken."""


def allocate_subnet(pool: str, prefix: int, used: Iterable[str]) -> str:
    """The first subnet of `prefix` bits in `pool` that overlaps none of `used`.

    Deterministic: the same pool and the same set give the same answer, so a restart that lost
    nothing allocates nothing new."""
    network = ipaddress.ip_network(pool, strict=True)
    taken = [ipaddress.ip_network(text, strict=True) for text in used]
    for candidate in network.subnets(new_prefix=prefix):
        if not any(candidate.overlaps(other) for other in taken):
            return str(candidate)
    raise NoFreeSubnetError(f"no free subnet of /{prefix} in {pool}")
