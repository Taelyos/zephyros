"""What the container filter builds: typed requests, fixed templates, and the checks on both.

The filter is a typed broker (ADR 0014). The hub does not send a container specification; it
sends a small request (a kind, a project, a side, a few validated values) and the filter builds
the specification from the templates below, from its own configuration. A value from a request
is never put in a name, an option or a path: names derive from the project identifier that the
hub issued and from enumerations, and the rest (an address, a token, a prompt) goes where the
template puts it and nowhere else.

`check_spec` is the allow-list of the permitted container operations and of the hardened
container profile (ADR 0014), written as code. The filter runs it on every specification before
sending it to the runtime, and the tests run it on every specification the templates can
produce.
"""

from __future__ import annotations

import copy
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zephyros.core.config import DEFAULT_EGRESS, DEFAULT_RESOLVERS, Config
from zephyros.core.ports.agent_engine import AgentEngine, Side, TurnRequest
from zephyros.core.principals import is_project_id

LABEL_MANAGED = "org.zephyros.managed"
LABEL_PROJECT = "org.zephyros.project"
LABEL_SIDE = "org.zephyros.side"
LABEL_ROLE = "org.zephyros.role"
LABEL_EGRESS = "org.zephyros.egress"
"""On a guard: the level its rules were loaded at (ADR 0024). The hub stops a guard whose level
is not the configured one, so a change of level never leaves an instance on the old rules."""
INSTANCE_USER = "10000:10000"
GUARD_USER = "10002:10002"
"""The user of a network guard: not root (ADR 0027). Its image gives nft, and nft alone, the file
capability NET_ADMIN, and lets only this group run it."""
GUARD_SECURITY = ["no-new-privileges:false"]
"""The one container without no-new-privileges, said in so many words so that a runtime whose
default is on does not take the guard's file capability away (ADR 0027): its bounding set is
NET_ADMIN alone, and its image holds no setuid program, so an exec gains nothing more."""
HOME_PATH = "/opt/data"
WORK_PATH = "/work"
MAX_PROMPT = 4000


class Role(StrEnum):
    NETWORK = "network"
    VOLUME = "volume"
    GUARD = "guard"
    TURN = "turn"
    SANDBOX = "sandbox"


class VolumeKind(StrEnum):
    HOME = "home"
    WORK = "work"


class SpecError(Exception):
    """A specification or a request that the policy does not allow."""


@dataclass(frozen=True, slots=True)
class Policy:
    """The configuration the filter builds containers from. It never comes from a request."""

    guard_image: str
    instance_image: str
    hub_container: str
    hub_port: int
    model: str
    cpus: float
    memory_mib: int
    pids: int
    guard_memory_mib: int
    network_pool: str
    network_prefix: int
    sandbox_images: tuple[tuple[str, str], ...] = ()
    """The image of each verification kit, as (kit, image)."""
    resolvers: tuple[str, ...] = DEFAULT_RESOLVERS
    """The name resolvers the guard of an instance gives to its network namespace, so that
    the instance can resolve names without reaching a protected network (ADR 0018)."""
    egress: str = DEFAULT_EGRESS
    """What an instance reaches besides the hub (ADR 0024): the guard loads the rules of this
    level, and a regulated turn is given the egress proxy of the hub."""
    egress_port: int = 8083

    def sandbox_image(self, kit: str) -> str:
        for name, image in self.sandbox_images:
            if name == kit:
                return image
        raise SpecError(f"no image for the kit {kit!r}")

    @classmethod
    def from_config(cls, config: Config) -> Policy:
        return cls(
            guard_image=config.images.guard,
            instance_image=config.images.instance,
            hub_container=config.runtime.hub_container,
            hub_port=config.hub.agent_port,
            model=config.gateway.model,
            cpus=config.limits.cpus,
            memory_mib=config.limits.memory_mib,
            pids=config.limits.pids,
            guard_memory_mib=config.limits.guard_memory_mib,
            network_pool=config.network.instance_pool,
            network_prefix=config.network.instance_prefix,
            resolvers=config.network.resolvers,
            egress=config.network.egress,
            egress_port=config.hub.egress_port,
            sandbox_images=(
                ((config.verification.kit, config.verification.kit_image),)
                if config.verification.kit_image
                else ()
            ),
        )


# ----------------------------------------------------------------------------- names
def _check_project(project_id: str) -> str:
    if not is_project_id(project_id):
        raise SpecError("not a project identifier")
    return project_id


def network_name(project_id: str, side: Side) -> str:
    return f"zs-net-{_check_project(project_id)}-{side.value}"


def volume_name(project_id: str, side: Side, kind: VolumeKind) -> str:
    return f"zs-vol-{_check_project(project_id)}-{side.value}-{kind.value}"


def guard_name(project_id: str, side: Side) -> str:
    return f"zs-guard-{_check_project(project_id)}-{side.value}"


def turn_name(project_id: str, side: Side) -> str:
    return f"zs-turn-{_check_project(project_id)}-{side.value}"


_RUN = re.compile(r"^[0-9a-f]{8}$")


def _check_run(run: str) -> str:
    if not _RUN.fullmatch(run):
        raise SpecError("not a run identifier")
    return run


def sandbox_name(project_id: str, run: str) -> str:
    """One sandbox, for one verification: its container and its volume have this name."""
    return f"zs-sbx-{_check_project(project_id)}-{_check_run(run)}"


def sandbox_labels(project_id: str, run: str) -> dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_ROLE: Role.SANDBOX.value,
        LABEL_PROJECT: _check_project(project_id),
        "org.zephyros.run": _check_run(run),
    }


def labels(role: Role, project_id: str, side: Side) -> dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_ROLE: role.value,
        LABEL_PROJECT: _check_project(project_id),
        LABEL_SIDE: side.value,
    }


# ----------------------------------------------------------------------------- addresses
def instance_subnet(policy: Policy, text: str) -> ipaddress.IPv4Network:
    """A subnet for an instance network: inside the pool, of the configured size."""
    try:
        subnet = ipaddress.ip_network(text, strict=True)
        pool = ipaddress.ip_network(policy.network_pool)
    except ValueError:
        raise SpecError("not an IPv4 network") from None
    if not isinstance(subnet, ipaddress.IPv4Network) or not isinstance(pool, ipaddress.IPv4Network):
        raise SpecError("the instance networks are IPv4")
    if subnet.prefixlen != policy.network_prefix or not subnet.subnet_of(pool):
        raise SpecError("the subnet is not one of the pool")
    return subnet


def guard_address(subnet: ipaddress.IPv4Network) -> ipaddress.IPv4Address:
    """The fixed address of the guard: the first host after the gateway."""
    return subnet.network_address + 2


def hub_address(subnet: ipaddress.IPv4Network) -> ipaddress.IPv4Address:
    """The fixed address of the hub on an instance network: the one after the guard's. Both are
    fixed so that neither can take the other's by the order in which they are attached."""
    return subnet.network_address + 3


def inside(subnet: ipaddress.IPv4Network, text: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        raise SpecError("not an IPv4 address") from None
    if address not in subnet:
        raise SpecError("the address is outside the subnet")
    return address


# ----------------------------------------------------------------------------- templates
def network_spec(project_id: str, side: Side, subnet: ipaddress.IPv4Network) -> dict[str, Any]:
    gateway = subnet.network_address + 1
    return {
        "Name": network_name(project_id, side),
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": False,
        "CheckDuplicate": True,
        "IPAM": {
            "Driver": "default",
            "Config": [{"Subnet": str(subnet), "Gateway": str(gateway)}],
        },
        "Labels": labels(Role.NETWORK, project_id, side),
    }


def volume_spec(project_id: str, side: Side, kind: VolumeKind) -> dict[str, Any]:
    return {
        "Name": volume_name(project_id, side, kind),
        "Driver": "local",
        "Labels": {**labels(Role.VOLUME, project_id, side), "org.zephyros.volume": kind.value},
    }


def sandbox_volume_spec(project_id: str, run: str) -> dict[str, Any]:
    return {
        "Name": sandbox_name(project_id, run),
        "Driver": "local",
        "Labels": sandbox_labels(project_id, run),
    }


def _hardening(memory_mib: int, pids: int, cpus: float) -> dict[str, Any]:
    memory = memory_mib * 1024 * 1024
    return {
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges"],
        "ReadonlyRootfs": True,
        "Privileged": False,
        "IpcMode": "none",
        "Init": True,
        "Memory": memory,
        "MemorySwap": memory,
        "PidsLimit": pids,
        "NanoCpus": int(cpus * 1_000_000_000),
        "RestartPolicy": {"Name": "no"},
        "AutoRemove": False,
    }


def guard_spec(
    policy: Policy, project_id: str, side: Side, subnet: ipaddress.IPv4Network, hub_address: str
) -> dict[str, Any]:
    """The network guard: the capability NET_ADMIN only, a fixed address, no listener and no
    secret. It runs as an unprivileged user; NET_ADMIN reaches it through the file capability of
    nft alone, which is why no-new-privileges is off here and nowhere else (ADR 0027)."""
    inside(subnet, hub_address)
    network = network_name(project_id, side)
    host = _hardening(policy.guard_memory_mib, 64, 0.5)
    host.update(
        {
            "CapAdd": ["NET_ADMIN"],
            "SecurityOpt": list(GUARD_SECURITY),
            "NetworkMode": network,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=1m"},  # noqa: S108  # reason: a container path
        }
    )
    if policy.resolvers:
        # The turn joins this network namespace, so its queries go to the resolvers the
        # runtime's own chain would otherwise send to the host's (possibly protected)
        # resolvers, which the rules below drop (ADR 0018).
        host["Dns"] = list(policy.resolvers)
    return {
        "Image": policy.guard_image,
        "User": GUARD_USER,
        "Env": [
            f"HUB_IP={hub_address}",
            f"HUB_PORT={policy.hub_port}",
            f"EGRESS={policy.egress}",
            f"PROXY_PORT={policy.egress_port}",
        ],
        "Labels": {**labels(Role.GUARD, project_id, side), LABEL_EGRESS: policy.egress},
        "Healthcheck": {
            "Test": ["CMD", "/usr/local/bin/guard.sh", "check"],
            "Interval": 1_000_000_000,
            "Timeout": 3_000_000_000,
            "Retries": 3,
            "StartPeriod": 0,
        },
        "HostConfig": host,
        "NetworkingConfig": {
            "EndpointsConfig": {
                network: {"IPAMConfig": {"IPv4Address": str(guard_address(subnet))}},
            }
        },
    }


SANDBOX_LOG: dict[str, Any] = {
    "Type": "json-file",
    "Config": {"max-size": "1t", "max-file": "1"},
}
"""The log of a sandbox is never rotated (ATK-22, ADR 0017). The filter believes what the kit
prints only in a log that begins with its run line; a runtime that rotates the log could drop that
head. The driver and a size no sandbox reaches are fixed here, since a log configuration that names
no size gets the daemon's own, which may rotate."""


def sandbox_spec(policy: Policy, project_id: str, run: str, kit: str) -> dict[str, Any]:
    """A sandbox (glossary): one verification, no network, no secret, the limits of an agent
    container, the files to verify in a volume of its own on /work, destroyed afterwards."""
    name = sandbox_name(project_id, run)
    host = _hardening(policy.memory_mib, policy.pids, policy.cpus)
    host.update(
        {
            "NetworkMode": "none",
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},  # noqa: S108  # reason: a container path
            "Mounts": [{"Type": "volume", "Source": name, "Target": WORK_PATH, "ReadOnly": False}],
            "LogConfig": copy.deepcopy(SANDBOX_LOG),
        }
    )
    return {
        "Image": policy.sandbox_image(kit),
        "User": INSTANCE_USER,
        "WorkingDir": WORK_PATH,
        "Env": ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"],
        "Labels": sandbox_labels(project_id, run),
        "HostConfig": host,
    }


_TOKEN = re.compile(r"^zph_[oc]_[A-Za-z0-9_-]{43}$")


_VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def turn_spec(
    policy: Policy,
    engine: AgentEngine,
    project_id: str,
    side: Side,
    hub_address: str,
    token: str,
    prompt: str,
) -> dict[str, Any]:
    """One turn of an instance: the agent engine as user 10000, in the namespace of the guard,
    with the home and work volumes and one secret, the project token. The engine gives the
    command and the environment; everything else is this template's."""
    if not _TOKEN.fullmatch(token):
        raise SpecError("not a project token")
    if not prompt or len(prompt) > MAX_PROMPT or "\x00" in prompt:
        raise SpecError("the prompt is empty, too long, or holds a NUL")
    ipaddress.IPv4Address(hub_address)
    description = engine.describe_turn(
        TurnRequest(
            project_id,
            side,
            f"http://{hub_address}:{policy.hub_port}",
            policy.model,
            token,
            prompt,
        )
    )
    command = list(description.command)
    environment = dict(description.environment)
    _check_description(command, environment, token)
    environment.update(proxy_environment(policy, hub_address, token))
    host = _hardening(policy.memory_mib, policy.pids, policy.cpus)
    host.update(
        {
            "NetworkMode": f"container:{guard_name(project_id, side)}",
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},  # noqa: S108  # reason: a container path
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": volume_name(project_id, side, VolumeKind.HOME),
                    "Target": HOME_PATH,
                    "ReadOnly": False,
                },
                {
                    "Type": "volume",
                    "Source": volume_name(project_id, side, VolumeKind.WORK),
                    "Target": WORK_PATH,
                    "ReadOnly": False,
                },
            ],
        }
    )
    return {
        "Image": policy.instance_image,
        "User": INSTANCE_USER,
        "WorkingDir": WORK_PATH,
        "Cmd": command,
        "Env": [f"{name}={value}" for name, value in sorted(environment.items())],
        "Labels": labels(Role.TURN, project_id, side),
        "HostConfig": host,
    }


def proxy_environment(policy: Policy, hub_address: str, token: str) -> dict[str, str]:
    """What sends the HTTP clients of a regulated turn through the egress proxy of the hub
    (ADR 0024): the proxy, in both spellings that clients read (curl reads the lower case
    only), and the hub itself kept off the proxy. The token authenticates the instance to the
    proxy as it does to the agent endpoint. Any other level gets nothing: a sealed instance has
    no proxy to reach, and an open one goes out directly."""
    if policy.egress != "regulated":
        return {}
    proxy = f"http://instance:{token}@{hub_address}:{policy.egress_port}"
    found: dict[str, str] = {}
    for name, value in (("http_proxy", proxy), ("https_proxy", proxy), ("no_proxy", hub_address)):
        found[name] = value
        found[name.upper()] = value
    return found


def _check_description(command: list[str], environment: dict[str, str], token: str) -> None:
    """An engine's description, checked as if it came from outside: the template does not trust
    an adapter to keep the secret off the command line."""
    if not command or not all(word and "\x00" not in word for word in command):
        raise SpecError("the engine gave an empty command, an empty word or a NUL")
    if any(token in word for word in command):
        raise SpecError("the engine put the token on the command line")
    for name, value in environment.items():
        if not _VARIABLE.fullmatch(name) or "\x00" in value:
            raise SpecError("the engine gave an environment variable that is not plain")
    if [name for name, value in environment.items() if token in value] == []:
        raise SpecError("the engine did not hand the token to the turn")


# ----------------------------------------------------------------------------- the allow-list
_FORBIDDEN_HOST_KEYS = (
    "Binds",
    "Devices",
    "DeviceCgroupRules",
    "DeviceRequests",
    "PidMode",
    "UsernsMode",
    "UTSMode",
    "CgroupnsMode",
    "VolumesFrom",
    "Links",
    "PortBindings",
    "PublishAllPorts",
    "ExtraHosts",
    "Sysctls",
    "Cgroup",
    "CgroupParent",
    "Runtime",
    "Isolation",
    "OomScoreAdj",
    "StorageOpt",
)


def check_spec(spec: Mapping[str, Any], role: Role, policy: Policy) -> None:
    """Raise SpecError unless `spec` is a container of the allowed kind with the full profile.

    This is the last gate before the runtime. It states the profile: no privileged mode, no
    host namespace, no bind of a host path, no device, no runtime socket, the capabilities
    dropped (and for the guard only NET_ADMIN kept), a read-only root file system, the limits,
    the default syscall filter (no override), and an image from the configuration.
    """
    if role not in (Role.GUARD, Role.TURN, Role.SANDBOX):
        raise SpecError("only a guard, a turn or a sandbox is a container")
    host = spec.get("HostConfig")
    if not isinstance(host, Mapping):
        raise SpecError("no host configuration")
    if role is Role.SANDBOX:
        allowed_images = {image for _kit, image in policy.sandbox_images}
    else:
        allowed_images = {policy.guard_image if role is Role.GUARD else policy.instance_image}
    if spec.get("Image") not in allowed_images:
        raise SpecError("the image is not the configured one")
    unknown = set(spec) - {
        "Image",
        "User",
        "WorkingDir",
        "Cmd",
        "Env",
        "Labels",
        "Healthcheck",
        "HostConfig",
        "NetworkingConfig",
    }
    if unknown:
        raise SpecError(f"unexpected fields: {sorted(unknown)}")
    for key in _FORBIDDEN_HOST_KEYS:
        if key in host:
            raise SpecError(f"{key} is not permitted")
    if host.get("Privileged") is not False:
        raise SpecError("privileged mode is not permitted")
    if host.get("CapDrop") != ["ALL"]:
        raise SpecError("all capabilities are dropped")
    allowed_add = ["NET_ADMIN"] if role is Role.GUARD else []
    if list(host.get("CapAdd", [])) != allowed_add:
        raise SpecError("capabilities other than the guard's NET_ADMIN are not permitted")
    if host.get("ReadonlyRootfs") is not True:
        raise SpecError("the root file system is read-only")
    if role is Role.GUARD:
        if spec.get("User") != GUARD_USER:
            raise SpecError("a guard runs as its unprivileged user, never as root")
        if host.get("SecurityOpt") != GUARD_SECURITY:
            raise SpecError("a guard has exactly the security options of its template")
    elif host.get("SecurityOpt") != ["no-new-privileges"]:
        raise SpecError("no-new-privileges, and no other security option, is required")
    if host.get("IpcMode") != "none":
        raise SpecError("the IPC mode is none")
    for limit in ("Memory", "PidsLimit", "NanoCpus"):
        if not isinstance(host.get(limit), int) or host[limit] <= 0:
            raise SpecError(f"{limit} must be a positive limit")
    mode = str(host.get("NetworkMode", ""))
    if role is Role.TURN and not mode.startswith("container:zs-guard-"):
        raise SpecError("a turn joins the namespace of its guard")
    if role is Role.GUARD and not mode.startswith("zs-net-"):
        raise SpecError("a guard joins its instance network")
    if role is Role.SANDBOX and mode != "none":
        raise SpecError("a sandbox has no network")
    if role is Role.GUARD and (
        f"EGRESS={policy.egress}" not in list(spec.get("Env", []))
        or not isinstance(spec.get("Labels"), Mapping)
        or spec["Labels"].get(LABEL_EGRESS) != policy.egress
    ):
        raise SpecError(
            "the guard loads, and is labelled with, the egress level of the configuration"
        )
    dns = host.get("Dns")
    if role is Role.GUARD:
        if list(dns or []) != list(policy.resolvers):
            raise SpecError("the guard takes exactly the configured name resolvers")
    elif dns:
        raise SpecError("only the guard carries the name resolvers of an instance")
    labels_ = spec.get("Labels")
    if not isinstance(labels_, Mapping) or labels_.get(LABEL_MANAGED) != "true":
        raise SpecError("a container carries the Zephyros labels")
    mounts = host.get("Mounts", [])
    if role is Role.GUARD and mounts:
        raise SpecError("a guard mounts nothing")
    prefix = "zs-sbx-" if role is Role.SANDBOX else "zs-vol-"
    for mount in mounts:
        if mount.get("Type") != "volume" or not str(mount.get("Source", "")).startswith(prefix):
            raise SpecError("only Zephyros volumes are mounted")
        if mount.get("Target") not in (HOME_PATH, WORK_PATH):
            raise SpecError("a volume is mounted on the home or the work path")
    if role is Role.SANDBOX:
        if spec.get("User") != INSTANCE_USER:
            raise SpecError("a sandbox runs as the unprivileged user")
        if len(mounts) != 1 or mounts[0].get("Target") != WORK_PATH:
            raise SpecError("a sandbox mounts its own volume on the work path, and nothing else")
        if set(host.get("Tmpfs", {})) != {"/tmp"}:  # noqa: S108  # reason: a container path
            raise SpecError("the temporary file system is /tmp")
        if host.get("LogConfig") != SANDBOX_LOG:
            raise SpecError("the log of a sandbox is never rotated")
    if role is Role.TURN:
        if spec.get("User") != INSTANCE_USER:
            raise SpecError("a turn runs as the unprivileged user")
        if len(mounts) != 2:
            raise SpecError("a turn mounts its home and its work volume")
        writable = {"/tmp"}  # noqa: S108  # reason: a container path
        if set(host.get("Tmpfs", {})) != writable:
            raise SpecError("the temporary file system is /tmp")
    for item in spec.get("Env", []):
        if "docker.sock" in str(item):
            raise SpecError("no runtime socket")
