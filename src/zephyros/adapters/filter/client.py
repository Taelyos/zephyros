"""The hub's side of the filter: the `Broker` interface over the Unix socket."""

from __future__ import annotations

from typing import Any

import httpx

from zephyros.core.broker import (
    BrokerError,
    ContainerInfo,
    Event,
    Op,
    Request,
    event_from_json,
    info_from_json,
    request_to_json,
)
from zephyros.core.containers import Role, VolumeKind
from zephyros.core.ports.agent_engine import Side

TIMEOUT_SECONDS = 90.0


class FilterClient:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    @classmethod
    def over_socket(cls, path: str) -> FilterClient:
        transport = httpx.HTTPTransport(uds=path)
        return cls(
            httpx.Client(transport=transport, base_url="http://filter", timeout=TIMEOUT_SECONDS)
        )

    def close(self) -> None:
        self._client.close()

    def _send(self, request: Request) -> dict[str, Any]:
        try:
            response = self._client.post("/operation", json=request_to_json(request))
        except httpx.HTTPError as error:
            raise BrokerError(
                "filter_unreachable", f"the filter could not be reached ({type(error).__name__})"
            ) from None
        try:
            body = response.json()
        except ValueError:
            raise BrokerError(
                "bad_reply", f"the filter answered HTTP {response.status_code}"
            ) from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            problem = body.get("error", {}) if isinstance(body, dict) else {}
            raise BrokerError(
                str(problem.get("code", "bad_reply")), str(problem.get("message", "no message"))
            )
        return dict(body.get("result", {}))

    def run_sandbox(self, archive: bytes, timeout_seconds: float) -> dict[str, Any]:
        """Run one verification in a sandbox (POST /sandbox). The archive holds the files and
        the manifest; the answer holds one outcome per test."""
        try:
            response = self._client.post(
                "/sandbox",
                content=archive,
                headers={"content-type": "application/x-tar"},
                timeout=timeout_seconds + 60,
            )
        except httpx.HTTPError as error:
            raise BrokerError(
                "filter_unreachable", f"the filter could not be reached ({type(error).__name__})"
            ) from None
        try:
            body = response.json()
        except ValueError:
            raise BrokerError(
                "bad_reply", f"the filter answered HTTP {response.status_code}"
            ) from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            problem = body.get("error", {}) if isinstance(body, dict) else {}
            raise BrokerError(
                str(problem.get("code", "bad_reply")), str(problem.get("message", "no message"))
            )
        return dict(body.get("result", {}))

    # ------------------------------------------------------------------ the Broker interface
    def create_network(self, project_id: str, side: Side, subnet: str) -> None:
        self._send(Request(Op.NETWORK_CREATE, project_id, side, subnet=subnet))

    def remove_network(self, project_id: str, side: Side) -> None:
        self._send(Request(Op.NETWORK_REMOVE, project_id, side))

    def connect_hub(self, project_id: str, side: Side) -> str:
        return str(self._send(Request(Op.HUB_CONNECT, project_id, side))["address"])

    def disconnect_hub(self, project_id: str, side: Side) -> None:
        self._send(Request(Op.HUB_DISCONNECT, project_id, side))

    def create_volume(self, project_id: str, side: Side, kind: VolumeKind) -> None:
        self._send(Request(Op.VOLUME_CREATE, project_id, side, volume=kind))

    def remove_volume(self, project_id: str, side: Side, kind: VolumeKind) -> None:
        self._send(Request(Op.VOLUME_REMOVE, project_id, side, volume=kind))

    def start_guard(self, project_id: str, side: Side, subnet: str) -> None:
        self._send(Request(Op.GUARD_START, project_id, side, subnet=subnet))

    def stop_guard(self, project_id: str, side: Side) -> None:
        self._send(Request(Op.GUARD_STOP, project_id, side))

    def start_turn(self, project_id: str, side: Side, token: str, prompt: str) -> None:
        self._send(Request(Op.TURN_START, project_id, side, token=token, prompt=prompt))

    def stop_turn(self, project_id: str, side: Side) -> None:
        self._send(Request(Op.TURN_STOP, project_id, side))

    def inspect(self, role: Role, project_id: str, side: Side) -> ContainerInfo | None:
        found = self._send(Request(Op.INSPECT, project_id, side, role=role))["container"]
        return None if found is None else info_from_json(found)

    def list_containers(self) -> list[ContainerInfo]:
        return [info_from_json(item) for item in self._send(Request(Op.LIST))["containers"]]

    def events(self, since: int) -> list[Event]:
        return [
            event_from_json(item) for item in self._send(Request(Op.EVENTS, since=since))["events"]
        ]
