"""A small fake of the Docker Engine API, in memory, behind an httpx transport.

It implements just the calls the filter makes, keeps every call it receives (so a test can say
"nothing reached the runtime"), and behaves like the real thing where the filter relies on it:
names are unique, a container gets an address on its network, a container that joins another's
namespace has none of its own, an object remembers its labels, events are recorded.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from zephyros.adapters.docker.api import DockerApi


@dataclass
class Call:
    method: str
    path: str
    body: Any = None
    params: dict[str, str] = field(default_factory=dict)


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"message": message})


def _stamp(moment: datetime) -> str:
    """A start time in the shape the runtime reports, to the second and the microseconds."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.networks: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.event_log: list[dict[str, Any]] = []
        self.clock = 1_000_000
        self.fail_start: set[str] = set()
        self.hold_health: set[str] = set()
        """Containers that stay 'starting' once started: a guard whose rules never load."""
        self.fail_next: dict[str, int] = {}
        self.archives: dict[str, list[tuple[str, bytes]]] = {}
        """For each container, the archives put into it, with the path they were put at."""
        self.kit: Callable[[bytes], tuple[int | None, str]] = lambda archive: (0, "")
        """What a sandbox does with its files: its exit code (None: still running at the end of
        the wait) and what it prints."""

    # ------------------------------------------------------------------ helpers for tests
    def api(self) -> DockerApi:
        client = httpx.Client(transport=httpx.MockTransport(self.handle), base_url="http://docker")
        return DockerApi(client)

    def add_hub(self, name: str = "zephyros-hub", managed: bool = True) -> None:
        labels = {"org.zephyros.managed": "true", "org.zephyros.role": "hub"} if managed else {}
        self.containers[name] = self._container(
            name, {"Image": "hub", "Labels": labels}, {"NetworkMode": "bridge"}
        )
        self.containers[name]["State"].update({"Status": "running", "Running": True})

    def add_foreign(self, kind: str, name: str, labels: dict[str, str] | None = None) -> None:
        """An object that is not Zephyros's (no labels, or wrong ones), possibly with a name the
        filter would use."""
        if kind == "network":
            self.networks[name] = {
                "Name": name,
                "Labels": labels or {},
                "IPAM": {"Config": []},
                "Containers": {},
            }
        elif kind == "volume":
            self.volumes[name] = {"Name": name, "Labels": labels or {}}
        else:
            self.containers[name] = self._container(
                name, {"Image": "x", "Labels": labels or {}}, {"NetworkMode": "bridge"}
            )

    def mutating_calls(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]

    def finish(self, name: str, code: int = 0) -> None:
        """The container's process exits."""
        container = self.containers[name]
        container["State"].update(
            {
                "Status": "exited",
                "Running": False,
                "ExitCode": code,
                "FinishedAt": "2026-10-10T12:30:00Z",
            }
        )
        self._event("die", container)

    def kill(self, name: str) -> None:
        """The container is killed from outside (out of memory, or by hand)."""
        self.finish(name, 137)

    def kill_health(self, name: str, status: str) -> None:
        self.containers[name]["State"]["Health"] = {"Status": status}

    def set_started(self, name: str, moment: datetime) -> None:
        """Set the start time the runtime reports: the reaper reads it to age a turn."""
        self.containers[name]["State"]["StartedAt"] = _stamp(moment)

    # ------------------------------------------------------------------ internals
    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def _event(self, action: str, container: dict[str, Any]) -> None:
        self.event_log.append(
            {
                "Type": "container",
                "Action": action,
                "Actor": {"ID": container["Id"], "Attributes": dict(container["Config"]["Labels"])},
                "time": self._tick(),
            }
        )

    def _container(self, name: str, config: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
        container: dict[str, Any] = {
            "Id": "c" + str(abs(hash(name)) % 10**12),
            "Name": "/" + name,
            "Config": {
                "Labels": dict(config.get("Labels") or {}),
                "Image": config.get("Image"),
                "User": config.get("User"),
                "Env": list(config.get("Env") or []),
                "Cmd": config.get("Cmd"),
            },
            "HostConfig": copy.deepcopy(host),
            "State": {
                "Status": "created",
                "Running": False,
                "ExitCode": 0,
                "StartedAt": "",
                "FinishedAt": "",
            },
            "NetworkSettings": {"Networks": {}},
        }
        if config.get("Healthcheck"):
            container["State"]["Health"] = {"Status": "starting"}
        return container

    def _next_address(self, network: str) -> str:
        subnet = ipaddress.ip_network(self.networks[network]["IPAM"]["Config"][0]["Subnet"])
        used = {
            ep["IPAddress"]
            for c in self.containers.values()
            for ep in c["NetworkSettings"]["Networks"].values()
            if ep.get("IPAddress")
        }
        for host in list(subnet.hosts())[1:]:
            if str(host) not in used:
                return str(host)
        raise RuntimeError("the subnet is full")

    def _attach(self, container: dict[str, Any], network: str, address: str | None = None) -> None:
        container["NetworkSettings"]["Networks"][network] = {
            "IPAddress": address or self._next_address(network)
        }

    # ------------------------------------------------------------------ the transport
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = re.sub(r"^/v[0-9.]+", "", request.url.path)
        raw = request.content
        typed = request.headers.get("content-type", "")
        body = raw if typed == "application/x-tar" else (json.loads(raw) if raw else None)
        params = dict(request.url.params)
        self.calls.append(Call(request.method, path, body, params))
        key = f"{request.method} {re.sub(r'/[^/]+', lambda m: m.group(0), path)}"
        if self.fail_next.get(key, 0) > 0:
            self.fail_next[key] -= 1
            return _error(500, "injected failure")
        return self._route(request.method, path, body, params)

    def _route(self, method: str, path: str, body: Any, params: dict[str, str]) -> httpx.Response:
        parts = [p for p in path.split("/") if p]
        if parts[:2] == ["networks", "create"] and method == "POST":
            return self._create_network(body)
        if parts[:1] == ["networks"] and len(parts) == 2 and method == "GET":
            network = self.networks.get(parts[1])
            return httpx.Response(200, json=network) if network else _error(404, "no such network")
        if parts[:1] == ["networks"] and len(parts) == 2 and method == "DELETE":
            return self._remove(self.networks, parts[1], "network")
        if parts[:1] == ["networks"] and len(parts) == 3 and parts[2] in ("connect", "disconnect"):
            return self._connect(parts[1], body, connect=parts[2] == "connect")
        if parts[:2] == ["volumes", "create"]:
            if body["Name"] in self.volumes:
                return httpx.Response(201, json=self.volumes[body["Name"]])
            self.volumes[body["Name"]] = {"Name": body["Name"], "Labels": body.get("Labels") or {}}
            return httpx.Response(201, json=self.volumes[body["Name"]])
        if parts[:1] == ["volumes"] and len(parts) == 2 and method == "GET":
            volume = self.volumes.get(parts[1])
            return httpx.Response(200, json=volume) if volume else _error(404, "no such volume")
        if parts[:1] == ["volumes"] and len(parts) == 2 and method == "DELETE":
            return self._remove(self.volumes, parts[1], "volume")
        if parts[:2] == ["containers", "create"]:
            return self._create_container(params["name"], body)
        if parts == ["containers", "json"]:
            return self._list(params)
        if parts[:1] == ["containers"] and len(parts) == 3 and parts[2] == "json":
            container = self.containers.get(parts[1])
            return (
                httpx.Response(200, json=container)
                if container
                else _error(404, "no such container")
            )
        if parts[:1] == ["containers"] and len(parts) == 3 and parts[2] == "start":
            return self._start(parts[1])
        if parts[:1] == ["containers"] and len(parts) == 3 and parts[2] == "stop":
            return self._stop(parts[1])
        if parts[:1] == ["containers"] and len(parts) == 3 and parts[2] == "kill":
            return self._stop(parts[1]) if parts[1] in self.containers else _error(404, "no")
        if parts[:1] == ["containers"] and len(parts) == 3 and parts[2] == "archive":
            if parts[1] not in self.containers:
                return _error(404, "no such container")
            self.archives.setdefault(parts[1], []).append((params["path"], body))
            return httpx.Response(200)
        if parts[:1] == ["containers"] and len(parts) == 3 and parts[2] == "wait":
            return self._wait(parts[1])
        if parts[:1] == ["containers"] and len(parts) == 3 and parts[2] == "logs":
            return self._logs(parts[1])
        if parts[:1] == ["containers"] and len(parts) == 2 and method == "DELETE":
            container = self.containers.pop(parts[1], None)
            if container is None:
                return _error(404, "no such container")
            self._event("destroy", container)
            return httpx.Response(204)
        if parts == ["events"]:
            return self._events(params)
        return _error(404, f"{method} {path} is not implemented by the fake")

    def _create_network(self, body: dict[str, Any]) -> httpx.Response:
        if body["Name"] in self.networks:
            return _error(409, "network with this name already exists")
        self.networks[body["Name"]] = {
            "Name": body["Name"],
            "Labels": body.get("Labels") or {},
            "IPAM": copy.deepcopy(body["IPAM"]),
            "Containers": {},
        }
        return httpx.Response(201, json={"Id": "n1"})

    def _remove(self, store: dict[str, Any], name: str, kind: str) -> httpx.Response:
        if name not in store:
            return _error(404, f"no such {kind}")
        if kind == "network" and any(
            name in c["NetworkSettings"]["Networks"] for c in self.containers.values()
        ):
            return _error(403, "network has active endpoints")
        del store[name]
        return httpx.Response(204)

    def _connect(self, network: str, body: dict[str, Any], *, connect: bool) -> httpx.Response:
        container = self.containers.get(body["Container"])
        if network not in self.networks or container is None:
            return _error(404, "not found")
        if connect:
            endpoint = body.get("EndpointConfig", {})
            self._attach(container, network, endpoint.get("IPAMConfig", {}).get("IPv4Address"))
        elif network in container["NetworkSettings"]["Networks"]:
            del container["NetworkSettings"]["Networks"][network]
        else:
            return _error(404, "container is not connected to the network")
        return httpx.Response(200)

    def _create_container(self, name: str, spec: dict[str, Any]) -> httpx.Response:
        if name in self.containers:
            return _error(409, "name already in use")
        container = self._container(name, spec, spec.get("HostConfig", {}))
        mode = spec.get("HostConfig", {}).get("NetworkMode", "")
        if mode in self.networks:
            endpoint = spec.get("NetworkingConfig", {}).get("EndpointsConfig", {}).get(mode, {})
            address = endpoint.get("IPAMConfig", {}).get("IPv4Address")
            self._attach(container, mode, address)
        elif mode.startswith("container:") and mode.split(":", 1)[1] not in self.containers:
            return _error(404, "no such container to join")
        self.containers[name] = container
        return httpx.Response(201, json={"Id": container["Id"]})

    def _start(self, name: str) -> httpx.Response:
        container = self.containers.get(name)
        if container is None:
            return _error(404, "no such container")
        if name in self.fail_start:
            return _error(500, "cannot start")
        if container["State"]["Running"]:
            return httpx.Response(304)
        container["State"].update(
            {"Status": "running", "Running": True, "StartedAt": _stamp(datetime.now(UTC))}
        )
        if "Health" in container["State"]:
            held = name in self.hold_health
            container["State"]["Health"] = {"Status": "starting" if held else "healthy"}
        self._event("start", container)
        return httpx.Response(204)

    def _stop(self, name: str) -> httpx.Response:
        container = self.containers.get(name)
        if container is None:
            return _error(404, "no such container")
        if not container["State"]["Running"]:
            return httpx.Response(304)
        container["State"].update({"Status": "exited", "Running": False, "ExitCode": 137})
        self._event("die", container)
        return httpx.Response(204)

    def _run_kit(self, name: str) -> tuple[int | None, str]:
        archives = self.archives.get(name, [])
        container = self.containers[name]
        if "result" not in container:
            container["result"] = self.kit(archives[-1][1] if archives else b"")
        result: tuple[int | None, str] = container["result"]
        return result

    def _wait(self, name: str) -> httpx.Response:
        if name not in self.containers:
            return _error(404, "no such container")
        code, _ = self._run_kit(name)
        if code is None:
            raise httpx.ReadTimeout("the container is still running")
        self.finish(name, code)
        return httpx.Response(200, json={"StatusCode": code})

    def _logs(self, name: str) -> httpx.Response:
        if name not in self.containers:
            return _error(404, "no such container")
        _, output = self._run_kit(name)
        data = output.encode()
        framed = bytes([1, 0, 0, 0]) + len(data).to_bytes(4, "big") + data if data else b""
        return httpx.Response(200, content=framed)

    def _list(self, params: dict[str, str]) -> httpx.Response:
        wanted = json.loads(params.get("filters", "{}")).get("label", [])
        items = []
        for name, container in self.containers.items():
            labels = container["Config"]["Labels"]
            if all(labels.get(w.split("=")[0]) == w.split("=")[1] for w in wanted if "=" in w):
                items.append({"Id": container["Id"], "Names": ["/" + name], "Labels": labels})
        return httpx.Response(200, json=items)

    def _events(self, params: dict[str, str]) -> httpx.Response:
        since, until = int(params["since"]), int(params["until"])
        wanted = json.loads(params.get("filters", "{}")).get("label", [])
        lines = []
        for event in self.event_log:
            labels = event["Actor"]["Attributes"]
            if since <= event["time"] <= until + 10**9 and all(
                labels.get(w.split("=")[0]) == w.split("=")[1] for w in wanted if "=" in w
            ):
                lines.append(json.dumps(event))
        return httpx.Response(200, text="\n".join(lines))
