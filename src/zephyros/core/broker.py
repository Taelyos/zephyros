"""The requests the hub may send the container filter, and the filter's answers (ADR 0014).

Each request is a kind plus a few validated values. There is no field for an image, a command,
an option, a mount, a network mode or a label: those come from the filter's templates. The
parser is strict. It refuses a kind that is not listed, a field that its kind does not have, a
field that is missing, a value outside its pattern, a body that is not a single JSON object,
and a key that appears twice.

The kinds are the container operations the hub may ask for (ADR 0014), in the form the hub
needs them: create a network, connect the hub to it, create the volumes, start the guard, start
and stop a turn, inspect and list Zephyros containers, and read their events.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from zephyros.core.containers import MAX_PROMPT, Role, VolumeKind
from zephyros.core.contract import TEST_ID
from zephyros.core.ports.agent_engine import Side
from zephyros.core.schema import Array, Choice, Field, Integer, Problem, Table, Text

MAX_BODY = 16 * 1024


class Op(StrEnum):
    NETWORK_CREATE = "network.create"
    NETWORK_REMOVE = "network.remove"
    HUB_CONNECT = "hub.connect"
    HUB_DISCONNECT = "hub.disconnect"
    VOLUME_CREATE = "volume.create"
    VOLUME_REMOVE = "volume.remove"
    GUARD_START = "guard.start"
    GUARD_STOP = "guard.stop"
    TURN_START = "turn.start"
    TURN_STOP = "turn.stop"
    INSPECT = "container.inspect"
    LIST = "container.list"
    EVENTS = "events"


class BrokerError(Exception):
    """The filter refused or could not do what was asked. `code` is stable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Request:
    op: Op
    project_id: str | None = None
    side: Side | None = None
    subnet: str | None = None
    volume: VolumeKind | None = None
    role: Role | None = None
    token: str | None = field(default=None, repr=False)
    prompt: str | None = field(default=None, repr=False)
    since: int | None = None
    session: str | None = None
    """For a turn: "new" starts a new session of the instance; unset or "continue" goes on."""


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    role: str
    project_id: str
    side: str
    state: str
    """created, running, exited or dead, as the runtime says."""
    health: str | None
    exit_code: int | None
    address: str | None
    started_at: str | None
    finished_at: str | None
    egress: str | None = None
    """For a guard, the level of its label (ADR 0024); None when it has none."""


@dataclass(frozen=True, slots=True)
class Event:
    time: int
    action: str
    role: str
    project_id: str
    side: str


_PROJECT = Field("project_id", Text(pattern=r"p_[a-z0-9]{6,32}"), "The project", required=True)
_SIDE = Field("side", Choice(("owner", "contractor")), "The side", required=True)
_SUBNET = Field("subnet", Text(pattern=r"[0-9.]{7,15}/[0-9]{1,2}"), "The subnet", required=True)


def _op(name: Op, *fields: Field) -> Table:
    return Table((Field("op", Choice((name.value,)), "The operation", required=True), *fields))


_SPECS: dict[Op, Table] = {
    Op.NETWORK_CREATE: _op(Op.NETWORK_CREATE, _PROJECT, _SIDE, _SUBNET),
    Op.NETWORK_REMOVE: _op(Op.NETWORK_REMOVE, _PROJECT, _SIDE),
    Op.HUB_CONNECT: _op(Op.HUB_CONNECT, _PROJECT, _SIDE),
    Op.HUB_DISCONNECT: _op(Op.HUB_DISCONNECT, _PROJECT, _SIDE),
    Op.VOLUME_CREATE: _op(
        Op.VOLUME_CREATE,
        _PROJECT,
        _SIDE,
        Field("volume", Choice(("home", "work")), "Which", required=True),
    ),
    Op.VOLUME_REMOVE: _op(
        Op.VOLUME_REMOVE,
        _PROJECT,
        _SIDE,
        Field("volume", Choice(("home", "work")), "Which", required=True),
    ),
    Op.GUARD_START: _op(Op.GUARD_START, _PROJECT, _SIDE, _SUBNET),
    Op.GUARD_STOP: _op(Op.GUARD_STOP, _PROJECT, _SIDE),
    Op.TURN_START: _op(
        Op.TURN_START,
        _PROJECT,
        _SIDE,
        Field(
            "token", Text(pattern=r"zph_[oc]_[A-Za-z0-9_-]{43}"), "The project token", required=True
        ),
        Field(
            "prompt",
            Text(min_length=1, max_length=MAX_PROMPT, multiline=True),
            "The first prompt",
            required=True,
        ),
        Field(
            "session",
            Choice(("continue", "new")),
            "Continue the session of the instance, or start a new one",
        ),
    ),
    Op.TURN_STOP: _op(Op.TURN_STOP, _PROJECT, _SIDE),
    Op.INSPECT: _op(
        Op.INSPECT,
        _PROJECT,
        _SIDE,
        Field("role", Choice(("guard", "turn")), "Which", required=True),
    ),
    Op.LIST: _op(Op.LIST),
    Op.EVENTS: _op(Op.EVENTS, Field("since", Integer(0, 2**40), "Unix time", required=True)),
}


SANDBOX_MANIFEST = Table(
    (
        _PROJECT,
        Field(
            "kit", Text(pattern=r"[a-z0-9][a-z0-9_-]{0,31}"), "The verification kit", required=True
        ),
        Field(
            "tests",
            Array(Text(max_length=240, pattern=TEST_ID), min_items=1, max_items=2000),
            "The acceptance tests to run",
            required=True,
        ),
        Field("timeout_seconds", Integer(1, 86400), "Longest run", required=True),
    )
)


@dataclass(frozen=True, slots=True)
class SandboxManifest:
    """What a sandbox run is asked for, read from `zephyros-request.json` in the tar."""

    project_id: str
    kit: str
    tests: tuple[str, ...]
    timeout_seconds: int


def parse_sandbox_manifest(raw: bytes) -> SandboxManifest:
    data = parse_body(raw)
    problems = SANDBOX_MANIFEST.check(data, "manifest")
    if problems:
        raise BrokerError("bad_request", "; ".join(str(p) for p in problems))
    tests = tuple(str(t) for t in data["tests"])
    if len(set(tests)) != len(tests):
        raise BrokerError("bad_request", "a test is named twice")
    return SandboxManifest(
        str(data["project_id"]), str(data["kit"]), tests, int(data["timeout_seconds"])
    )


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError("duplicate_key", f"the key {key!r} appears twice")
        result[key] = value
    return result


def parse_body(raw: bytes) -> Mapping[str, Any]:
    """The JSON object in a body, or a BrokerError. Strict: size, encoding, one object, no
    repeated key, no NaN."""
    if len(raw) > MAX_BODY:
        raise BrokerError("too_large", "the body is too large")
    try:
        text = raw.decode("utf-8")
        data = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_refuse_constant,
        )
    except BrokerError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise BrokerError("bad_json", "the body is not valid JSON") from None
    if not isinstance(data, dict):
        raise BrokerError("bad_request", "the body is one JSON object")
    return data


def _refuse_constant(name: str) -> None:
    raise BrokerError("bad_json", f"{name} is not allowed")


def parse_request(data: Mapping[str, Any]) -> Request:
    """Validate a request body against the closed shape of its kind."""
    name = data.get("op")
    try:
        op = Op(name) if isinstance(name, str) else None
    except ValueError:
        op = None
    if op is None:
        raise BrokerError("unknown_operation", "this operation is not permitted")
    problems: list[Problem] = _SPECS[op].check(data, "request")
    if problems:
        raise BrokerError("bad_request", "; ".join(str(p) for p in problems))
    side = Side(str(data["side"])) if "side" in data else None
    return Request(
        op=op,
        project_id=data.get("project_id"),
        side=side,
        subnet=data.get("subnet"),
        volume=VolumeKind(str(data["volume"])) if "volume" in data else None,
        role=Role(str(data["role"])) if "role" in data else None,
        token=data.get("token"),
        prompt=data.get("prompt"),
        since=data.get("since"),
        session=data.get("session"),
    )


def request_to_json(request: Request) -> dict[str, Any]:
    """The body for a request, as the hub sends it."""
    body: dict[str, Any] = {"op": request.op.value}
    for name in ("project_id", "subnet", "token", "prompt", "since", "session"):
        value = getattr(request, name)
        if value is not None:
            body[name] = value
    for name in ("side", "volume", "role"):
        value = getattr(request, name)
        if value is not None:
            body[name] = value.value
    return body


def info_to_json(info: ContainerInfo) -> dict[str, Any]:
    return {
        "role": info.role,
        "project_id": info.project_id,
        "side": info.side,
        "state": info.state,
        "health": info.health,
        "exit_code": info.exit_code,
        "address": info.address,
        "started_at": info.started_at,
        "finished_at": info.finished_at,
        "egress": info.egress,
    }


def info_from_json(data: Mapping[str, Any]) -> ContainerInfo:
    return ContainerInfo(
        role=str(data["role"]),
        project_id=str(data["project_id"]),
        side=str(data["side"]),
        state=str(data["state"]),
        health=None if data.get("health") is None else str(data["health"]),
        exit_code=None if data.get("exit_code") is None else int(data["exit_code"]),
        address=None if data.get("address") is None else str(data["address"]),
        started_at=None if data.get("started_at") is None else str(data["started_at"]),
        finished_at=None if data.get("finished_at") is None else str(data["finished_at"]),
        egress=None if data.get("egress") is None else str(data["egress"]),
    )


def event_to_json(event: Event) -> dict[str, Any]:
    return {
        "time": event.time,
        "action": event.action,
        "role": event.role,
        "project_id": event.project_id,
        "side": event.side,
    }


def event_from_json(data: Mapping[str, Any]) -> Event:
    return Event(
        time=int(data["time"]),
        action=str(data["action"]),
        role=str(data["role"]),
        project_id=str(data["project_id"]),
        side=str(data["side"]),
    )


class Broker(Protocol):
    """What the hub asks of the container runtime, through the filter."""

    def create_network(self, project_id: str, side: Side, subnet: str) -> None: ...

    def remove_network(self, project_id: str, side: Side) -> None: ...

    def connect_hub(self, project_id: str, side: Side) -> str:
        """Attach the hub to the instance network. Returns the hub's address on it."""
        ...

    def disconnect_hub(self, project_id: str, side: Side) -> None: ...

    def create_volume(self, project_id: str, side: Side, kind: VolumeKind) -> None: ...

    def remove_volume(self, project_id: str, side: Side, kind: VolumeKind) -> None: ...

    def start_guard(self, project_id: str, side: Side, subnet: str) -> None: ...

    def stop_guard(self, project_id: str, side: Side) -> None: ...

    def start_turn(
        self, project_id: str, side: Side, token: str, prompt: str, *, new_session: bool = False
    ) -> None: ...

    def stop_turn(self, project_id: str, side: Side) -> None: ...

    def inspect(self, role: Role, project_id: str, side: Side) -> ContainerInfo | None: ...

    def list_containers(self) -> list[ContainerInfo]: ...

    def events(self, since: int) -> list[Event]: ...
