"""The world of the runtime tests: a real container runtime, the real filter, real guards.

Instances are brought up by the filter's executor, with its templates and its allow-list, over
the runtime's socket: what runs is exactly what the hub would run, except that the agent engine
is a probe image that waits, so that the attempts can be run inside it with the hardening of a
real turn. The hub is a stand-in that records every peer (it plays the agent endpoint on port
8080, and has a second port, 9090, that no instance may reach).

The Docker command line is used only to observe and to set up fixtures (the stand-in of the
local network, the controls that take the guard's rules off). Every object carries the label
`org.zephyros.test` and is removed at the end.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zephyros.adapters.docker.api import DockerApi
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.core.broker import Op, Request
from zephyros.core.containers import (
    Policy,
    Role,
    VolumeKind,
    check_spec,
    guard_name,
    network_name,
    sandbox_name,
    sandbox_spec,
    sandbox_volume_spec,
    turn_name,
    volume_name,
)
from zephyros.core.instances import allocate_subnet
from zephyros.core.ports.agent_engine import Side

ROOT = Path(__file__).resolve().parents[2]
SOCKET = os.environ.get("ZEPHYROS_DOCKER_SOCKET", "/var/run/docker.sock")
POOL = "10.213.0.0/16"
HUB_PORT = 8080
CORE_SUBNET = "11.22.33.0/24"
"""The core network of a fake-DNS world: a fixed subnet, so the resolver has a known address.
It is deliberately outside every protected range: a configured resolver in a protected network
is refused at startup (ADR 0018), and the scripted resolver is a test fixture, not a loophole."""
FAKE_RESOLVER = "11.22.33.53"
OTHER_HUB_PORT = 9090
RESOLVERS = tuple(
    part.strip()
    for part in os.environ.get("ZEPHYROS_RESOLVERS", "9.9.9.9").split(",")
    if part.strip()
)
"""The name resolvers of the guards under test, as the deployment gives them (ADR 0018):
public ones, so that name resolution works while the protected networks stay blocked."""
TOKEN = "zph_c_" + "R" * 43
LAN_ADDRESSES = (
    "10.77.0.5",
    "172.31.250.5",
    "192.168.77.5",
    "169.254.169.254",
    "100.64.7.7",
    "100.100.100.200",
)


def docker(
    *args: str, check: bool = True, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # reason: a fixed program, arguments from the tests
        ["docker", *args],  # noqa: S607  # reason: the runtime's own command, found on PATH
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def build(tag: str, context: Path) -> str:
    docker("build", "-q", "-t", tag, str(context), timeout=900)
    return docker("image", "inspect", "-f", "{{.Id}}", tag).stdout.strip()


@dataclass
class Instance:
    project_id: str
    side: Side
    subnet: ipaddress.IPv4Network
    guard_ip: str
    hub_ip: str

    @property
    def guard(self) -> str:
        return guard_name(self.project_id, self.side)

    @property
    def turn(self) -> str:
        return turn_name(self.project_id, self.side)

    @property
    def network(self) -> str:
        return network_name(self.project_id, self.side)


@dataclass
class Runtime:
    images: dict[str, str]
    suffix: str = field(default_factory=lambda: secrets.token_hex(3))
    instances: list[Instance] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    sandbox_volumes: list[str] = field(default_factory=list)
    fake_dns: bool = False
    """A scripted resolver on the core network, at a known address (decision 38)."""

    def __post_init__(self) -> None:
        self.label = f"org.zephyros.test={self.suffix}"
        self.core = f"zs-test-core-{self.suffix}"
        self.hub = f"zs-test-hub-{self.suffix}"
        self.policy = Policy(
            guard_image=self.images["guard"],
            instance_image=self.images["probe"],
            hub_container=self.hub,
            hub_port=HUB_PORT,
            model="zephyros-default",
            cpus=1.0,
            memory_mib=256,
            pids=128,
            guard_memory_mib=32,
            network_pool=POOL,
            network_prefix=28,
            sandbox_images=(("tests", self.images["probe"]),),
            resolvers=(FAKE_RESOLVER,) if self.fake_dns else RESOLVERS,
        )
        self.journal = MemoryJournal()
        self.executor = Executor(DockerApi.over_socket(SOCKET), self.policy, self.journal)

    # ------------------------------------------------------------------ setup and teardown
    def setup(self) -> None:
        create = ["network", "create", "--label", self.label, self.core]
        if self.fake_dns:
            create += ["--subnet", CORE_SUBNET]
        docker(*create)
        if self.fake_dns:
            docker(
                "run",
                "-d",
                "--name",
                f"zs-test-dns-{self.suffix}",
                "--network",
                self.core,
                "--ip",
                FAKE_RESOLVER,
                "--label",
                self.label,
                self.images["dns"],
            )
        docker(
            "run",
            "-d",
            "--name",
            self.hub,
            "--network",
            self.core,
            "--label",
            self.label,
            "--label",
            "org.zephyros.managed=true",
            "--label",
            "org.zephyros.role=hub",
            "-e",
            "OBS_NAME=hub",
            self.images["observer"],
            f"tcp:{HUB_PORT}",
            f"tcp:{OTHER_HUB_PORT}",
            f"udp:{HUB_PORT}",
        )

    def teardown(self) -> None:
        for instance in self.instances:
            for name in (instance.turn, instance.guard):
                docker("rm", "-f", name, check=False)
        for name in [*self.fixtures, self.hub]:
            docker("rm", "-f", name, check=False)
        for found in docker(
            "ps", "-aq", "--filter", f"label={self.label}", check=False
        ).stdout.split():
            docker("rm", "-f", found, check=False)
        for instance in self.instances:
            docker("network", "rm", instance.network, check=False)
            for kind in VolumeKind:
                docker(
                    "volume",
                    "rm",
                    volume_name(instance.project_id, instance.side, kind),
                    check=False,
                )
        for found in docker(
            "network", "ls", "-q", "--filter", f"label={self.label}", check=False
        ).stdout.split():
            docker("network", "rm", found, check=False)
        for volume in self.sandbox_volumes:
            docker("volume", "rm", "-f", volume, check=False)

    # ------------------------------------------------------------------ instances
    def project(self) -> str:
        return f"p_rt{self.suffix}{secrets.token_hex(2)}"

    def bring_up(self, project_id: str, side: Side, *, turn: bool = True) -> Instance:
        used = [str(i.subnet) for i in self.instances] + self._docker_subnets()
        subnet = ipaddress.IPv4Network(allocate_subnet(POOL, 28, used))

        def ask(op: Op, **fields: Any) -> dict[str, Any]:
            return self.executor.perform(Request(op, project_id, side, **fields))

        ask(Op.NETWORK_CREATE, subnet=str(subnet))
        hub_ip = str(ask(Op.HUB_CONNECT)["address"])
        for kind in VolumeKind:
            ask(Op.VOLUME_CREATE, volume=kind)
        ask(Op.GUARD_START, subnet=str(subnet))
        instance = Instance(project_id, side, subnet, str(subnet.network_address + 2), hub_ip)
        self.instances.append(instance)
        if self.fake_dns:
            # The scripted resolver stands in for a public resolver (decision 38): the guard
            # joins the core network so the address is routed like a public one, and the
            # guard's rules treat it as they treat any non-protected destination.
            docker("network", "connect", self.core, instance.guard)
        self.wait_healthy(instance)
        if turn:
            ask(Op.TURN_START, token=TOKEN, prompt="Work.")
            self.wait_running(instance.turn)
        return instance

    def sandbox(
        self,
        project_id: str,
        *,
        run: str = "00000001",
        command: tuple[str, ...] = ("sleep", "900"),
    ) -> str:
        """A sandbox container from the template of the filter itself (sandbox_spec), started
        with a waiting command so that the attacks can be run inside it. The fixtures carry the
        rig's label and are removed at teardown."""
        name = sandbox_name(project_id, run)
        volume = sandbox_volume_spec(project_id, run)
        api = DockerApi.over_socket(SOCKET)
        try:
            api.create_volume(volume)
            spec = sandbox_spec(self.policy, project_id, run, "tests")
            spec["Cmd"] = list(command)
            check_spec(spec, Role.SANDBOX, self.policy)
            api.create_container(name, spec)
            api.start(name)
        finally:
            api.close()
        self.fixtures.append(name)
        self.sandbox_volumes.append(volume["Name"])
        return name

    def wait_healthy(self, instance: Instance, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            info = self.executor.perform(
                Request(
                    Op.INSPECT, project_id=instance.project_id, side=instance.side, role=Role.GUARD
                )
            )["container"]
            if info and info["health"] == "healthy":
                return
            time.sleep(0.3)
        raise AssertionError(f"the guard of {instance.project_id} never became healthy")

    def wait_running(self, name: str, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = docker("inspect", "-f", "{{.State.Running}}", name, check=False).stdout.strip()
            if state == "true":
                return
            time.sleep(0.2)
        raise AssertionError(f"{name} is not running")

    def _docker_subnets(self) -> list[str]:
        subnets: list[str] = []
        for network in docker("network", "ls", "-q").stdout.split():
            data = json.loads(docker("network", "inspect", network).stdout)[0]
            configs = (data.get("IPAM") or {}).get("Config") or []
            subnets.extend(c["Subnet"] for c in configs if "." in c.get("Subnet", ""))
        return subnets

    # ------------------------------------------------------------------ observing and acting
    def run_in(
        self, container: str, *command: str, user: str | None = None, timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        options = ["-u", user] if user else []
        return docker("exec", *options, container, *command, check=False, timeout=timeout)

    def sh(
        self, container: str, script: str, *, user: str | None = None, timeout: float = 60
    ) -> tuple[int, str]:
        done = self.run_in(container, "sh", "-c", script, user=user, timeout=timeout)
        return done.returncode, (done.stdout + done.stderr).strip()

    def connect(self, container: str, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        done = self.run_in(container, "probe", "connect", json.dumps(targets))
        assert done.returncode == 0, done.stderr
        results: list[dict[str, Any]] = json.loads(done.stdout)
        return results

    def reach(self, container: str, proto: str, host: str, port: int) -> str:
        return str(
            self.connect(container, [{"proto": proto, "host": host, "port": port}])[0]["result"]
        )

    def hits(self, container: str) -> list[str]:
        logs = docker("logs", container, check=False)
        return [
            line
            for line in (logs.stdout + logs.stderr).splitlines()
            if line.startswith(("TCP ", "UDP "))
        ]

    def rules_off(self, instance: Instance) -> None:
        docker("exec", instance.guard, "nft", "delete", "table", "inet", "zeph")

    def rules_on(self, instance: Instance) -> None:
        docker("exec", instance.guard, "/usr/local/bin/guard.sh", "load")

    def ip_of(self, container: str, network: str) -> str:
        template = '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}'
        return docker("inspect", "-f", template, container).stdout.strip()

    def lan(self, instance: Instance) -> str:
        """A stand-in of the local network on the instance network: it answers on the protected
        addresses of LAN_ADDRESSES, routed through it from the guard's namespace."""
        name = f"zs-test-lan-{instance.project_id}-{instance.side.value}"
        aliases = "; ".join(f"ip addr add {a}/32 dev lo" for a in LAN_ADDRESSES)
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            instance.network,
            "--label",
            self.label,
            "--cap-add",
            "NET_ADMIN",
            "-e",
            "OBS_NAME=lan",
            "--entrypoint",
            "sh",
            self.images["observer"],
            "-c",
            f"{aliases}; exec python -u /observer.py tcp:80 udp:80 tcp:7000",
        )
        self.fixtures.append(name)
        lan_ip = self.ip_of(name, instance.network)
        for address in LAN_ADDRESSES:
            docker("exec", instance.guard, "ip", "route", "add", f"{address}/32", "via", lan_ip)
        time.sleep(1)
        return name

    def has_ipv6(self, container: str) -> bool:
        code, out = self.sh(container, "ip -6 route show default 2>/dev/null")
        return code == 0 and bool(out.strip())
