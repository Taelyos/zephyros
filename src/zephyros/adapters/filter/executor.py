"""Carries out a validated request on the container runtime, on Zephyros objects only.

Every object the filter touches is recognized by the labels the filter itself set when it
created it. An operation on an object without them is refused, and a read returns nothing about
such an object. The hub's own container is the one exception: it is named in the configuration,
carries the label role=hub, and is only ever attached to and detached from instance networks.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Mapping
from typing import Any

from zephyros.adapters.docker.api import DockerApi, DockerError
from zephyros.adapters.engines import ENGINES
from zephyros.adapters.filter.journal import Journal
from zephyros.adapters.filter.sandbox import run_sandbox, sweep_sandboxes
from zephyros.core.broker import (
    BrokerError,
    ContainerInfo,
    Event,
    Op,
    Request,
    SandboxManifest,
    info_to_json,
)
from zephyros.core.containers import (
    LABEL_EGRESS,
    LABEL_MANAGED,
    LABEL_PROJECT,
    LABEL_ROLE,
    LABEL_SIDE,
    Policy,
    Role,
    SpecError,
    VolumeKind,
    check_spec,
    guard_name,
    guard_spec,
    hub_address,
    instance_subnet,
    network_name,
    network_spec,
    turn_name,
    turn_spec,
    volume_name,
    volume_spec,
)
from zephyros.core.ports.agent_engine import AgentEngine, Side

STOP_SECONDS = 2
WATCHED_ACTIONS = ("start", "die", "kill", "oom", "stop", "destroy")
MANAGED_FILTER = f"{LABEL_MANAGED}=true"
HUB_ROLE = "hub"


def _labels(data: Mapping[str, Any]) -> Mapping[str, str]:
    raw = data.get("Labels")
    if raw is None:
        raw = (data.get("Config") or {}).get("Labels")
    return raw if isinstance(raw, Mapping) else {}


def _owned(data: Mapping[str, Any] | None, role: Role, project_id: str, side: Side) -> bool:
    if data is None:
        return False
    found = _labels(data)
    return (
        found.get(LABEL_MANAGED) == "true"
        and found.get(LABEL_ROLE) == role.value
        and found.get(LABEL_PROJECT) == project_id
        and found.get(LABEL_SIDE) == side.value
    )


class Executor:
    def __init__(
        self,
        api: DockerApi,
        policy: Policy,
        journal: Journal,
        *,
        clock: Callable[[], float] = time.time,
        engine: AgentEngine | None = None,
    ) -> None:
        """`engine`, when given, runs the turns of both sides (tests); otherwise each side runs
        the engine the policy names for it."""
        self._engines = {
            side: engine or ENGINES[policy.engine(side)]() for side in (Side.OWNER, Side.CONTRACTOR)
        }
        self._api = api
        self._policy = policy
        self._journal = journal
        self._clock = clock

    def run_sandbox(self, manifest: SandboxManifest, archive: bytes) -> dict[str, Any]:
        """One verification in a sandbox of its own (see adapters/filter/sandbox.py)."""
        return run_sandbox(self._api, self._policy, self._journal, manifest, archive)

    def sweep_sandboxes(self) -> list[str]:
        """The sandboxes a stopped filter left behind, removed (see adapters/filter/sandbox.py)."""
        return sweep_sandboxes(self._api, self._journal)

    # ------------------------------------------------------------------ entry point
    def perform(self, request: Request) -> dict[str, Any]:
        """Do what the request says, or raise BrokerError. The answer is plain data."""
        try:
            result = self._dispatch(request)
        except BrokerError as error:
            self._journal.record("refused", request.op.value, error.code, **self._subject(request))
            raise
        except SpecError as error:
            self._journal.record("refused", request.op.value, "policy", **self._subject(request))
            raise BrokerError("policy", str(error)) from None
        except DockerError as error:
            self._journal.record(
                "failed", request.op.value, "runtime_error", **self._subject(request)
            )
            message = str(error)
            for secret in (request.token, request.prompt):
                if secret:
                    message = message.replace(secret, "[redacted]")
            raise BrokerError("runtime_error", message) from None
        self._journal.record(
            "done", request.op.value, "ok", **self._subject(request), **self._applied(request)
        )
        return result

    def _applied(self, request: Request) -> dict[str, str]:
        """For a turn, what the filter gave it from its own configuration and the request: the
        log of the hub records what the hub meant, this what the turn got (ADR 0033)."""
        if request.op is not Op.TURN_START:
            return {}
        context = self._policy.context
        return {
            "session": "new" if request.session == "new" else "continue",
            "context_window": str(context.window),
            "compaction_tokens": str(context.compaction),
            "reasoning_echo": "true" if self._policy.reasoning_echo else "false",
        }

    @staticmethod
    def _subject(request: Request) -> dict[str, str]:
        subject: dict[str, str] = {}
        if request.project_id:
            subject["project"] = request.project_id
        if request.side:
            subject["side"] = request.side.value
        return subject

    def _dispatch(self, request: Request) -> dict[str, Any]:
        operations: dict[Op, Callable[[Request], dict[str, Any]]] = {
            Op.NETWORK_CREATE: self._network_create,
            Op.NETWORK_REMOVE: self._network_remove,
            Op.HUB_CONNECT: self._hub_connect,
            Op.HUB_DISCONNECT: self._hub_disconnect,
            Op.VOLUME_CREATE: self._volume_create,
            Op.VOLUME_REMOVE: self._volume_remove,
            Op.GUARD_START: self._guard_start,
            Op.GUARD_STOP: self._guard_stop,
            Op.TURN_START: self._turn_start,
            Op.TURN_STOP: self._turn_stop,
            Op.INSPECT: self._inspect,
            Op.LIST: self._list,
            Op.EVENTS: self._events,
        }
        return operations[request.op](request)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _who(request: Request) -> tuple[str, Side]:
        assert request.project_id is not None  # noqa: S101  # reason: the parser requires it
        assert request.side is not None  # noqa: S101  # reason: the parser requires it
        return request.project_id, request.side

    def _network(self, project_id: str, side: Side) -> dict[str, Any] | None:
        data = self._api.inspect_network(network_name(project_id, side))
        if data is not None and not _owned(data, Role.NETWORK, project_id, side):
            raise BrokerError(
                "not_zephyros", "the network is not a Zephyros network of this instance"
            )
        return data

    def _subnet_of(self, project_id: str, side: Side) -> ipaddress.IPv4Network:
        network = self._network(project_id, side)
        configured = [c.get("Subnet") for c in (network or {}).get("IPAM", {}).get("Config", [])]
        if len(configured) != 1:
            raise BrokerError("conflict", "the network does not have exactly one subnet")
        return instance_subnet(self._policy, str(configured[0]))

    def _hub_address(self, project_id: str, side: Side) -> str:
        hub = self._api.inspect(self._policy.hub_container)
        found = _labels(hub or {})
        if hub is None or found.get(LABEL_MANAGED) != "true" or found.get(LABEL_ROLE) != HUB_ROLE:
            raise BrokerError(
                "hub_missing", "the hub container is not here or is not marked as the hub"
            )
        endpoint = (
            hub.get("NetworkSettings", {}).get("Networks", {}).get(network_name(project_id, side))
        )
        address = (endpoint or {}).get("IPAddress")
        if not address:
            raise BrokerError(
                "hub_not_connected", "the hub is not attached to this instance network"
            )
        return str(ipaddress.IPv4Address(address))

    def _container(
        self, name: str, role: Role, project_id: str, side: Side
    ) -> dict[str, Any] | None:
        data = self._api.inspect(name)
        if data is not None and not _owned(data, role, project_id, side):
            raise BrokerError(
                "not_zephyros", "the container is not a Zephyros container of this instance"
            )
        return data

    def _stop_and_remove(
        self, name: str, role: Role, project_id: str, side: Side
    ) -> dict[str, Any]:
        data = self._container(name, role, project_id, side)
        if data is None:
            return {"was_present": False}
        if data.get("State", {}).get("Running"):
            self._api.stop(name, STOP_SECONDS)
        self._api.remove(name)
        return {"was_present": True}

    # ------------------------------------------------------------------ networks
    def _network_create(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        subnet = instance_subnet(self._policy, str(request.subnet))
        existing = self._network(project_id, side)
        if existing is not None:
            configured = [c.get("Subnet") for c in existing.get("IPAM", {}).get("Config", [])]
            if configured != [str(subnet)]:
                raise BrokerError("conflict", "the network exists with another subnet")
            return {"created": False}
        self._api.create_network(network_spec(project_id, side, subnet))
        return {"created": True}

    def _network_remove(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        if self._network(project_id, side) is None:
            return {"was_present": False}
        self._api.remove_network(network_name(project_id, side))
        return {"was_present": True}

    def _hub_connect(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        if self._network(project_id, side) is None:
            raise BrokerError("not_found", "there is no such network")
        try:
            self._hub_address(project_id, side)
        except BrokerError as error:
            if error.code != "hub_not_connected":
                raise
            self._api.connect(
                network_name(project_id, side),
                self._policy.hub_container,
                str(hub_address(self._subnet_of(project_id, side))),
            )
        return {"address": self._hub_address(project_id, side)}

    def _hub_disconnect(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        if self._network(project_id, side) is None:
            return {"was_connected": False}
        try:
            self._hub_address(project_id, side)
        except BrokerError as error:
            if error.code == "hub_not_connected":
                return {"was_connected": False}
            raise
        self._api.disconnect(network_name(project_id, side), self._policy.hub_container)
        return {"was_connected": True}

    # ------------------------------------------------------------------ volumes
    def _volume(self, project_id: str, side: Side, kind: VolumeKind) -> dict[str, Any] | None:
        data = self._api.inspect_volume(volume_name(project_id, side, kind))
        if data is not None and not _owned(data, Role.VOLUME, project_id, side):
            raise BrokerError(
                "not_zephyros", "the volume is not a Zephyros volume of this instance"
            )
        return data

    def _volume_create(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        kind = request.volume or VolumeKind.HOME
        if self._volume(project_id, side, kind) is not None:
            return {"created": False}
        self._api.create_volume(volume_spec(project_id, side, kind))
        return {"created": True}

    def _volume_remove(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        kind = request.volume or VolumeKind.HOME
        if self._volume(project_id, side, kind) is None:
            return {"was_present": False}
        self._api.remove_volume(volume_name(project_id, side, kind))
        return {"was_present": True}

    # ------------------------------------------------------------------ guard and turn
    def _guard_start(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        subnet = instance_subnet(self._policy, str(request.subnet))
        network = self._network(project_id, side)
        if network is None:
            raise BrokerError("not_found", "there is no such network")
        configured = [c.get("Subnet") for c in network.get("IPAM", {}).get("Config", [])]
        if configured != [str(subnet)]:
            raise BrokerError("conflict", "the subnet is not the one of the network")
        spec = guard_spec(
            self._policy, project_id, side, subnet, self._hub_address(project_id, side)
        )
        check_spec(spec, Role.GUARD, self._policy)
        name = guard_name(project_id, side)
        if self._container(name, Role.GUARD, project_id, side) is not None:
            raise BrokerError("conflict", "the guard exists")
        self._api.create_container(name, spec)
        try:
            self._api.start(name)
        except DockerError:
            self._api.remove(name)
            raise
        return {}

    def _guard_stop(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        return self._stop_and_remove(guard_name(project_id, side), Role.GUARD, project_id, side)

    def _turn_start(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        guard = self._container(guard_name(project_id, side), Role.GUARD, project_id, side)
        if guard is None or not guard.get("State", {}).get("Running"):
            raise BrokerError("not_ready", "the guard of the instance is not running")
        spec = turn_spec(
            self._policy,
            self._engines[side],
            project_id,
            side,
            self._hub_address(project_id, side),
            str(request.token),
            str(request.prompt),
            new_session=request.session == "new",
        )
        check_spec(spec, Role.TURN, self._policy)
        name = turn_name(project_id, side)
        if self._container(name, Role.TURN, project_id, side) is not None:
            raise BrokerError("conflict", "a turn exists: stop it first")
        self._api.create_container(name, spec)
        try:
            self._api.start(name)
        except DockerError:
            self._api.remove(name)
            raise
        return {}

    def _turn_stop(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        return self._stop_and_remove(turn_name(project_id, side), Role.TURN, project_id, side)

    # ------------------------------------------------------------------ reads
    def _info(self, data: Mapping[str, Any]) -> ContainerInfo | None:
        found = _labels(data)
        if found.get(LABEL_MANAGED) != "true" or found.get(LABEL_ROLE) not in (
            Role.GUARD.value,
            Role.TURN.value,
        ):
            return None
        state = data.get("State", {})
        networks = data.get("NetworkSettings", {}).get("Networks", {}) or {}
        address = next((n.get("IPAddress") for n in networks.values() if n.get("IPAddress")), None)
        health = (state.get("Health") or {}).get("Status")
        return ContainerInfo(
            role=str(found[LABEL_ROLE]),
            project_id=str(found.get(LABEL_PROJECT, "")),
            side=str(found.get(LABEL_SIDE, "")),
            state=str(state.get("Status", "unknown")),
            health=None if health is None else str(health),
            exit_code=state.get("ExitCode") if state.get("Status") in ("exited", "dead") else None,
            address=None if address is None else str(address),
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt"),
            egress=found.get(LABEL_EGRESS) if found.get(LABEL_ROLE) == Role.GUARD.value else None,
        )

    def _inspect(self, request: Request) -> dict[str, Any]:
        project_id, side = self._who(request)
        role = request.role or Role.GUARD
        name = guard_name(project_id, side) if role is Role.GUARD else turn_name(project_id, side)
        data = self._container(name, role, project_id, side)
        info = None if data is None else self._info(data)
        return {"container": None if info is None else info_to_json(info)}

    def _list(self, _request: Request) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for summary in self._api.list_containers(MANAGED_FILTER):
            names = summary.get("Names") or []
            if not names:
                continue
            data = self._api.inspect(str(names[0]).lstrip("/"))
            info = None if data is None else self._info(data)
            if info is not None:
                items.append(info_to_json(info))
        return {"containers": items}

    def _events(self, request: Request) -> dict[str, Any]:
        now = int(self._clock())
        found: list[Event] = []
        for raw in self._api.events(int(request.since or 0), now, MANAGED_FILTER):
            attributes = (raw.get("Actor") or {}).get("Attributes") or {}
            action = str(raw.get("Action", "")).split(":")[0]
            role = attributes.get(LABEL_ROLE)
            if attributes.get(LABEL_MANAGED) != "true" or role not in (
                Role.GUARD.value,
                Role.TURN.value,
            ):
                continue
            if action not in (*WATCHED_ACTIONS, "health_status"):
                continue
            found.append(
                Event(
                    int(raw.get("time", 0)),
                    str(raw.get("Action", "")),
                    str(role),
                    str(attributes.get(LABEL_PROJECT, "")),
                    str(attributes.get(LABEL_SIDE, "")),
                )
            )
        return {
            "events": [
                {
                    "time": e.time,
                    "action": e.action,
                    "role": e.role,
                    "project_id": e.project_id,
                    "side": e.side,
                }
                for e in found
            ],
            "now": now,
        }
