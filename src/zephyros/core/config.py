"""The configuration of the hub: its schema, its version and its typed form (EXG-50).

The schema is published as `schemas/configuration.schema.json` and carries a version number.
`parse_config` validates data that an adapter read from a file and returns it as typed,
frozen objects. A violation raises `ConfigError`, which lists every faulty key.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from zephyros.core.schema import (
    CORE_ADDRESS,
    Array,
    Choice,
    Field,
    Flag,
    Integer,
    Number,
    Problem,
    Rule,
    Schema,
    Table,
    Text,
)

CONFIG_SCHEMA_VERSION = "1.15.0"

CONFIG_SCHEMA_ID = "https://github.com/Taelyos/zephyros/schemas/configuration.schema.json"

SCHEMA_VERSION_KEY = "schema_version"

DEFAULT_RESOLVERS: tuple[str, ...] = ("9.9.9.9",)
"""The name resolvers an instance uses when the configuration names none.

The instances are IPv4 only, and the network guard blocks every address of a protected
network, so the resolver has to be a public one. Quad9 (9.9.9.9) is chosen as the default:
a public resolver that filters malicious domains and does not log the client address. The
Owner chose it on 2026-10-12; see ADR 0018."""

EGRESS_LEVELS: tuple[str, ...] = ("regulated", "sealed", "open")
"""The network access levels of an instance (ADR 0024): `regulated` reaches the hub and the
allowed hosts through the egress proxy of the hub; `sealed` reaches the hub alone; `open`
reaches every public destination, as ADR 0009 had it. No level reaches a protected network."""

DEFAULT_EGRESS = "regulated"

DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)
"""The hosts a `regulated` instance reaches when the configuration names none: the package
registries, for reading. GitHub is not one of them: an operator who wants it adds it."""

HOST_NAME = r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]"
"""A host name as the allow-list holds it: lower case, at least two labels, no trailing dot,
no port, no wildcard, and a last label that is not a number (so an address is not a name)."""

PROTECTED_V4: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
)
"""The IPv4 networks of annex "Protected networks" of the statement of work, as the guard
loads them (images/guard/guard.sh). The guard drops every connection to them, so a name
resolver inside one of them can never work from an instance; the schema refuses it."""


class ConfigError(Exception):
    """The configuration violates the schema. `problems` names each faulty key."""

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = tuple(problems)
        lines = "\n".join(f"  {problem}" for problem in problems)
        super().__init__(f"the configuration violates the schema:\n{lines}")


def _hub() -> Table:
    return Table(
        (
            Field(
                "agent_port",
                Integer(1024, 65535),
                "Port of the agent endpoint, the one hub "
                "destination open to instances (/mcp and /v1)",
                default=8080,
            ),
            Field(
                "operator_port", Integer(1024, 65535), "Port of the operator channel", default=8081
            ),
            Field(
                "main_agent_port",
                Integer(1024, 65535),
                "Port of the main-agent endpoint",
                default=8082,
            ),
            Field(
                "operator_bind",
                Text("listen-address"),
                "Address the operator channel listens on: an address that is neither a wildcard "
                'nor one of the instance pool, or "core", the address of the hub outside the '
                "instance networks (the one its port is published on) and its own loopback "
                "(ADR 0025)",
                default="127.0.0.1",
            ),
            Field(
                "main_agent_bind",
                Text("listen-address"),
                "Address the main-agent endpoint listens on; the same forms as hub.operator_bind",
                default="127.0.0.1",
            ),
            Field(
                "egress_port",
                Integer(1024, 65535),
                "Port of the egress proxy, the second hub destination open to a regulated "
                "instance: it relays a connection to an allowed host and logs every destination "
                "asked for (ADR 0024)",
                default=8083,
            ),
        )
    )


def _paths() -> Table:
    return Table(
        (
            Field(
                "data_dir",
                Text("absolute-path"),
                "Directory of the state of the hub; mounted in no execution context",
                default="/var/lib/zephyros",
            ),
            Field(
                "filter_socket",
                Text("absolute-path"),
                "Unix socket of the container filter",
                default="/run/zephyros/filter.sock",
            ),
            Field(
                "operator_token_file",
                Text("absolute-path"),
                "File that holds the operator "
                "token, readable by the operating-system user of the human only",
                default="/var/lib/zephyros/operator.token",
            ),
            Field(
                "main_agent_token_file",
                Text("absolute-path"),
                "File that holds the main-agent token",
                default="/var/lib/zephyros/main-agent.token",
            ),
            Field(
                "control_socket",
                Text("absolute-path", max_length=100),
                "Unix socket, open to the user of the hub only, on which `zephyros hub "
                "rotate-token` asks the running hub for a new operator or main-agent token "
                "(ADR 0026)",
                default="/var/lib/zephyros/control.sock",
            ),
        )
    )


def _runtime() -> Table:
    return Table(
        (
            Field(
                "core_network",
                Text("name"),
                "Network of the hub and the filter",
                default="zephyros-core",
            ),
            Field(
                "hub_container",
                Text("name"),
                "Name of the hub container, which the filter attaches to each instance network",
                default="zephyros-hub",
            ),
            Field(
                "guard_health_seconds",
                Integer(5, 600),
                "How long the hub waits for a network guard to report healthy; "
                "after that the instance does not start",
                default=60,
            ),
            Field(
                "poll_seconds",
                Integer(1, 60),
                "How often the hub looks at its instances: a turn that ended or ran too long, "
                "a guard that stopped",
                default=5,
            ),
        )
    )


def _images() -> Table:
    return Table(
        (
            Field(
                "instance",
                Text("image-digest"),
                "Image of an agent container, by digest",
                required=True,
            ),
            Field(
                "guard",
                Text("image-digest"),
                "Image of the network guard, by digest",
                required=True,
            ),
        )
    )


def _network() -> Table:
    return Table(
        (
            Field(
                "instance_pool",
                Text("ipv4-network"),
                "Range the subnets of the instance "
                "networks are taken from; it overlaps no network of the user",
                default="10.200.0.0/16",
            ),
            Field(
                "instance_prefix",
                Integer(24, 30),
                "Prefix length of the subnet of one instance network",
                default=28,
            ),
            Field(
                "resolvers",
                Array(Text("ipv4-address"), min_items=1, max_items=4),
                "Name resolvers an open instance uses (a regulated or sealed one sends no "
                "query; the egress proxy resolves for it); an address of a protected network "
                "is refused, because the network guard blocks it",
                default=list(DEFAULT_RESOLVERS),
            ),
            Field(
                "egress",
                Choice(EGRESS_LEVELS),
                "What an instance reaches besides the hub (ADR 0024): regulated, the allowed "
                "hosts through the egress proxy of the hub; sealed, nothing else; open, every "
                "public destination. No level reaches a protected network",
                default=DEFAULT_EGRESS,
            ),
            Field(
                "allowed_hosts",
                Array(Text(max_length=253, pattern=HOST_NAME), min_items=1, max_items=64),
                "Host names a regulated instance reaches on port 443 through the egress proxy; "
                "a name, never an address or a wildcard",
                default=list(DEFAULT_ALLOWED_HOSTS),
            ),
        )
    )


def _continuity() -> Table:
    return Table(
        (
            Field(
                "inactivity_seconds",
                Integer(60, 86_400),
                "A task without activity for this long: wake the instance, then restart it "
                "(US-E7-01)",
                default=900,
            ),
            Field(
                "restarts",
                Integer(1, 10),
                "How many restarts before the main agent is alerted",
                default=2,
            ),
            Field(
                "unblock_seconds",
                Integer(60, 86_400),
                "How long the main agent has to unblock a task before the human is alerted",
                default=3600,
            ),
            Field(
                "rejections",
                Integer(1, 50),
                "FAIL decisions in a row on one project before the main agent is alerted (EXG-23)",
                default=3,
            ),
        )
    )


def _notifications() -> Table:
    return Table(
        (
            Field(
                "url",
                Text(max_length=2048),
                "Where the hub posts its signed notifications; empty turns them off",
                default="",
            ),
            Field(
                "secret",
                Text(max_length=512),
                "Shared secret the receiver verifies the signatures with (EXG-43); "
                "at least 16 characters when a url is set",
                default="",
            ),
        )
    )


def _limits() -> Table:
    return Table(
        (
            Field("cpus", Number(0.1, 16.0), "CPUs of an agent container", default=2.0),
            Field(
                "memory_mib",
                Integer(128, 65536),
                "Memory of an agent container, in MiB (a real turn of the second real run "
                "reached 1019 MiB)",
                default=1536,
            ),
            Field(
                "pids",
                Integer(32, 4096),
                "Processes and threads of an agent container",
                default=256,
            ),
            Field(
                "guard_memory_mib",
                Integer(16, 512),
                "Memory of a network guard, in MiB",
                default=32,
            ),
            Field("turn_seconds", Integer(60, 86400), "Longest duration of a turn", default=7200),
            Field(
                "silence_seconds",
                Integer(30, 86400),
                "A turn that has called the hub, the model included, none of the time for this "
                "long is stopped",
                default=600,
            ),
            Field(
                "refusals_per_minute",
                Integer(10, 100_000),
                "Refused actions of one instance in a minute before the hub answers without "
                "doing the work (ATK-71)",
                default=120,
            ),
        )
    )


def _tokens() -> Table:
    return Table(
        (
            Field(
                "lifetime_seconds",
                Integer(120, 604800),
                "Lifetime of a project token; longer than the longest turn",
                default=14400,
            ),
            Field(
                "rotate_before_seconds",
                Integer(30, 86400),
                "A turn that starts when less than this remains of the lifetime gets a new token",
                default=900,
            ),
            Field(
                "channel_lifetime_days",
                Integer(1, 3650),
                "Lifetime of the operator token and of the main-agent token; a token issued "
                "before this key existed gets it from the next start of the hub (ADR 0026)",
                default=90,
            ),
        )
    )


def _gateway() -> Table:
    return Table(
        (
            Field(
                "provider",
                Choice(("stub", "openai-compatible")),
                "Model provider behind the "
                "gateway: the obedient stub, or an OpenAI-compatible service",
                default="stub",
            ),
            Field(
                "upstream_url",
                Text("http-url"),
                "Base URL of the provider, when it is openai-compatible",
            ),
            Field(
                "credential_file",
                Text("absolute-path"),
                "File that holds the model credential, when the provider is openai-compatible",
            ),
            Field(
                "model",
                Text("name"),
                "Name of the model instances ask for",
                default="zephyros-default",
            ),
            Field(
                "upstream_model",
                Text(max_length=100, pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*"),
                "Name of the model at the provider, when it differs from the one instances ask for",
            ),
            Field(
                "models",
                Array(
                    Text(max_length=100, pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*"),
                    min_items=1,
                    max_items=8,
                ),
                "The models the gateway may serve at the provider, when it is openai-compatible: "
                "the model it sends (gateway.upstream_model, or gateway.model when that is "
                "unset) must be one of them, and the hub refuses to start for any other. This "
                "list is the model policy of the hub: naming one model authorises that one alone",
            ),
            Field(
                "session_header",
                Text(max_length=80, pattern=r"[A-Za-z0-9-]+"),
                "Name of the session header the gateway sends to the provider, when it asks for "
                "one (OpenCode Go requires x-opencode-session); unset sends none. The value is one "
                "stable session name per hub process",
                default=None,
            ),
            Field(
                "timeout_seconds",
                Integer(10, 3600),
                "How long the gateway waits for one answer of the provider, in seconds, when it is "
                "openai-compatible: a long answer of a real model takes minutes",
                default=600,
            ),
            Field(
                "simulation",
                Flag(),
                "Run with the simulated provider and mark the report, the pull request and the "
                "events as a simulation (EXG-46)",
                default=False,
            ),
        )
    )


def _forge() -> Table:
    return Table(
        (
            Field(
                "provider",
                Choice(("none", "directory", "github")),
                "Where pull requests are opened: none, the forge in a directory (bare git "
                "repositories, for the development deployment and the tests), or GitHub (the "
                "repositories of the person, through its API and git over https)",
                default="none",
            ),
            Field(
                "root",
                Text("absolute-path"),
                "Directory of the forge, when the provider is directory",
                default="/var/lib/zephyros/forge",
            ),
            Field(
                "token_file",
                Text("absolute-path"),
                "File of the token GitHub is called with, when the provider is github; read at "
                "each call, never copied and never written down",
                default=None,
            ),
        )
    )


def _verification() -> Table:
    return Table(
        (
            Field(
                "runner",
                Choice(("none", "sandbox")),
                "What runs the acceptance tests: none (every verification ends in an error), or a "
                "sandbox created through the container filter",
                default="none",
            ),
            Field("kit", Text("name"), "The verification kit of the projects", default="tests"),
            Field(
                "kit_image",
                Text("image-digest"),
                "The image of that kit, by digest, when the runner is sandbox",
            ),
            Field(
                "timeout_seconds",
                Integer(30, 3600),
                "Longest run of the acceptance tests of one delivery",
                default=300,
            ),
            Field(
                "protected_paths",
                Array(
                    Text(max_length=150, pattern=r"(?!.*\.\.)[A-Za-z0-9_.][A-Za-z0-9_./-]*"),
                    min_items=1,
                    max_items=32,
                ),
                "Paths of the client's own files (a path, or a directory ending with /): a "
                "delivery that changes one is flagged in its report and its pull request, never "
                "refused",
                default=["CONTRACT.md"],
            ),
        )
    )


def _flag(table: Mapping[str, object], key: str) -> bool:
    value = table[key]
    return value if isinstance(value, bool) else False


def _int(table: Mapping[str, object], key: str) -> int:
    return cast("int", table[key])


def _section(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    return cast("Mapping[str, object]", data[key])


def _rule_ports(data: Mapping[str, object]) -> list[Problem]:
    hub = _section(data, "hub")
    seen: dict[int, str] = {}
    problems: list[Problem] = []
    for key in ("agent_port", "operator_port", "main_agent_port", "egress_port"):
        port = _int(hub, key)
        if port in seen:
            problems.append(Problem(f"hub.{key}", f"uses the port of hub.{seen[port]}"))
        else:
            seen[port] = key
    return problems


def _rule_token_lifetime(data: Mapping[str, object]) -> list[Problem]:
    tokens = _section(data, "tokens")
    limits = _section(data, "limits")
    problems: list[Problem] = []
    lifetime = _int(tokens, "lifetime_seconds")
    if lifetime <= _int(limits, "turn_seconds"):
        problems.append(
            Problem("tokens.lifetime_seconds", "must be longer than limits.turn_seconds")
        )
    if _int(tokens, "rotate_before_seconds") >= lifetime:
        problems.append(
            Problem("tokens.rotate_before_seconds", "must be shorter than tokens.lifetime_seconds")
        )
    return problems


def _rule_pool(data: Mapping[str, object]) -> list[Problem]:
    network = _section(data, "network")
    pool = ipaddress.ip_network(cast("str", network["instance_pool"]))
    prefix = _int(network, "instance_prefix")
    problems: list[Problem] = []
    if prefix <= pool.prefixlen:
        problems.append(
            Problem(
                "network.instance_prefix", "must be longer than the prefix of the instance pool"
            )
        )
    return problems


def _rule_binds(data: Mapping[str, object]) -> list[Problem]:
    """An address of the instance pool is an address of an instance network: the operator
    channel and the main-agent endpoint never listen there (ATK-13, ADR 0025)."""
    hub = _section(data, "hub")
    pool = ipaddress.ip_network(cast("str", _section(data, "network")["instance_pool"]))
    problems: list[Problem] = []
    for key in ("operator_bind", "main_agent_bind"):
        value = cast("str", hub[key])
        if value == CORE_ADDRESS:
            continue
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped  # ::ffff:10.200.4.3 is 10.200.4.3
        if address.version == pool.version and address in pool:
            problems.append(
                Problem(f"hub.{key}", "is an address of network.instance_pool, an instance network")
            )
    return problems


def _rule_resolvers(data: Mapping[str, object]) -> list[Problem]:
    """A resolver of a protected network can never work: the guard drops every connection
    to it before it leaves the instance. Refusing it at startup beats a silent failure at
    the first model call (EXG-50)."""
    network = _section(data, "network")
    protected = [ipaddress.ip_network(item) for item in PROTECTED_V4]
    problems: list[Problem] = []
    for value in cast("list[str]", network.get("resolvers") or []):
        address = ipaddress.ip_address(value)
        if any(address in item for item in protected):
            problems.append(
                Problem(
                    f"network.resolvers[{value}]",
                    'is in a protected network (annex "Protected networks"): the network '
                    "guard blocks it, so an instance could not reach it",
                )
            )
    return problems


def _rule_simulation(data: Mapping[str, object]) -> list[Problem]:
    """A simulation mark belongs to the simulated provider: an openai-compatible provider is a
    real model, and its artefacts must not be marked as a simulation (EXG-46)."""
    gateway = _section(data, "gateway")
    if _flag(gateway, "simulation") and gateway.get("provider") != "stub":
        return [
            Problem(
                "gateway.simulation",
                "runs only with the stub provider: a real provider is not a simulation",
            )
        ]
    return []


def _rule_notifications(data: Mapping[str, object]) -> list[Problem]:
    notifications = _section(data, "notifications")
    url = str(notifications.get("url", ""))
    secret = str(notifications.get("secret", ""))
    if not url:
        if secret:
            return [
                Problem(
                    "notifications.url",
                    "is empty while notifications.secret is set: set both, or neither",
                )
            ]
        return []
    problems: list[Problem] = []
    if not url.startswith(("http://", "https://")):
        problems.append(Problem("notifications.url", "must start with http:// or https://"))
    if len(secret) < 16:
        problems.append(
            Problem(
                "notifications.secret",
                "is shorter than 16 characters; it signs every delivery",
            )
        )
    return problems


def _rule_sandbox(data: Mapping[str, object]) -> list[Problem]:
    verification = _section(data, "verification")
    if verification.get("runner") == "sandbox" and "kit_image" not in verification:
        return [
            Problem("verification.kit_image", "is required when verification.runner is sandbox")
        ]
    return []


def _rule_forge(data: Mapping[str, object]) -> list[Problem]:
    forge = _section(data, "forge")
    if forge["provider"] != "github":
        return []
    if "token_file" not in forge:
        return [
            Problem(
                "forge.token_file",
                "names the file of the token GitHub is called with: it is required when "
                "forge.provider is github",
            )
        ]
    return []


def _rule_gateway(data: Mapping[str, object]) -> list[Problem]:
    gateway = _section(data, "gateway")
    if gateway["provider"] != "openai-compatible":
        return []
    problems = [
        Problem(f"gateway.{key}", "is required when gateway.provider is openai-compatible")
        for key in ("upstream_url", "credential_file")
        if key not in gateway
    ]
    models = cast("list[str]", gateway.get("models") or [])
    if not models:
        problems.append(
            Problem(
                "gateway.models",
                "names the models the gateway may serve at the provider: it is required when "
                "gateway.provider is openai-compatible, and the model the gateway sends to the "
                "provider must be one of them",
            )
        )
        return problems
    # The key to name is the one that sets what goes to the provider: gateway.upstream_model
    # when the file holds it, gateway.model otherwise.
    if "upstream_model" in gateway:
        key, upstream, how = "gateway.upstream_model", str(gateway["upstream_model"]), ""
    else:
        key, upstream = "gateway.model", str(gateway.get("model", ""))
        how = " goes to the provider as it is (gateway.upstream_model is unset) and"
    if upstream not in models:
        problems.append(
            Problem(
                key,
                f"{upstream!r}{how} is not one of gateway.models ({', '.join(models)}): the "
                "gateway serves only the models its configuration names",
            )
        )
    return problems


SCHEMA = Schema(
    identifier=CONFIG_SCHEMA_ID,
    version=CONFIG_SCHEMA_VERSION,
    title="Zephyros configuration",
    root=Table(
        (
            Field(
                SCHEMA_VERSION_KEY,
                Text("version"),
                "Version of this schema the file was written for",
                required=True,
            ),
            Field("hub", _hub(), "Listeners of the hub"),
            Field("paths", _paths(), "Files and directories"),
            Field("runtime", _runtime(), "Container runtime objects"),
            Field("images", _images(), "Images, by digest", required=True),
            Field("network", _network(), "Instance networks"),
            Field("limits", _limits(), "Limits of the containers and of a turn"),
            Field("tokens", _tokens(), "Project tokens"),
            Field("gateway", _gateway(), "Model gateway"),
            Field("forge", _forge(), "Forge"),
            Field("verification", _verification(), "Verification of deliveries"),
            Field("notifications", _notifications(), "Signed notifications to the main agent"),
            Field("continuity", _continuity(), "The escalation ladder of a task without activity"),
        )
    ),
    rules=(
        Rule(
            "hub.agent_port, hub.operator_port, hub.main_agent_port and hub.egress_port are four "
            "different ports",
            _rule_ports,
        ),
        Rule(
            "tokens.lifetime_seconds is longer than limits.turn_seconds, and "
            "tokens.rotate_before_seconds is shorter than tokens.lifetime_seconds",
            _rule_token_lifetime,
        ),
        Rule(
            "network.instance_prefix is longer than the prefix of network.instance_pool", _rule_pool
        ),
        Rule(
            "hub.operator_bind and hub.main_agent_bind are not addresses of network.instance_pool",
            _rule_binds,
        ),
        Rule(
            "network.resolvers holds no address of a protected network (the guard blocks them)",
            _rule_resolvers,
        ),
        Rule(
            "forge.token_file is set when forge.provider is github",
            _rule_forge,
        ),
        Rule(
            "gateway.upstream_url and gateway.credential_file are set when gateway.provider "
            "is openai-compatible, and the model it sends to the provider "
            "(gateway.upstream_model, or gateway.model when that is unset) is one of "
            "gateway.models",
            _rule_gateway,
        ),
        Rule("verification.kit_image is set when verification.runner is sandbox", _rule_sandbox),
        Rule(
            "gateway.simulation runs only with the stub provider",
            _rule_simulation,
        ),
        Rule(
            "notifications.url and notifications.secret are set together, the url is http(s), "
            "and the secret is at least 16 characters",
            _rule_notifications,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class HubConfig:
    agent_port: int
    operator_port: int
    main_agent_port: int
    operator_bind: str
    main_agent_bind: str
    egress_port: int = 8083


@dataclass(frozen=True, slots=True)
class PathsConfig:
    data_dir: str
    filter_socket: str
    operator_token_file: str
    main_agent_token_file: str
    control_socket: str


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    core_network: str
    hub_container: str
    guard_health_seconds: int
    poll_seconds: int


@dataclass(frozen=True, slots=True)
class ImagesConfig:
    instance: str
    guard: str


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    instance_pool: str
    instance_prefix: int
    resolvers: tuple[str, ...]
    egress: str = DEFAULT_EGRESS
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    cpus: float
    memory_mib: int
    pids: int
    guard_memory_mib: int
    turn_seconds: int
    silence_seconds: int
    refusals_per_minute: int


@dataclass(frozen=True, slots=True)
class TokensConfig:
    lifetime_seconds: int
    rotate_before_seconds: int
    channel_lifetime_days: int


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    provider: str
    model: str
    upstream_model: str
    models: tuple[str, ...]
    upstream_url: str | None
    credential_file: str | None
    session_header: str | None
    simulation: bool
    timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    provider: str
    root: str
    token_file: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    runner: str
    kit: str
    timeout_seconds: int
    kit_image: str | None
    protected_paths: tuple[str, ...] = ("CONTRACT.md",)


@dataclass(frozen=True, slots=True)
class Config:
    hub: HubConfig
    paths: PathsConfig
    runtime: RuntimeConfig
    images: ImagesConfig
    network: NetworkConfig
    limits: LimitsConfig
    tokens: TokensConfig
    gateway: GatewayConfig
    forge: ForgeConfig
    verification: VerificationConfig
    notifications: NotificationsConfig
    continuity: ContinuityConfig


@dataclass(frozen=True, slots=True)
class NotificationsConfig:
    url: str
    secret: str


@dataclass(frozen=True, slots=True)
class ContinuityConfig:
    inactivity_seconds: int
    restarts: int
    unblock_seconds: int
    rejections: int


def _check_version(data: object) -> list[Problem]:
    if isinstance(data, Mapping):
        declared = data.get(SCHEMA_VERSION_KEY)
        if (
            isinstance(declared, str)
            and declared.split(".")[0] != CONFIG_SCHEMA_VERSION.split(".")[0]
        ):
            return [
                Problem(
                    SCHEMA_VERSION_KEY,
                    f"version {declared} is not supported; this hub reads {CONFIG_SCHEMA_VERSION}",
                )
            ]
    return []


def parse_config(data: object) -> Config:
    """Validate `data` against the schema and return the typed configuration.

    Raises ConfigError, naming each faulty key, and returns nothing on a violation.
    """
    problems = SCHEMA.validate(data)
    if not problems:
        problems = _check_version(data)
    if problems:
        raise ConfigError(problems)
    filled = SCHEMA.with_defaults(cast("Mapping[str, object]", data))

    def section(name: str) -> Mapping[str, object]:
        return cast("Mapping[str, object]", filled[name])

    hub, paths, runtime = section("hub"), section("paths"), section("runtime")
    images, network, limits = section("images"), section("network"), section("limits")
    tokens, gateway = section("tokens"), section("gateway")
    forge, verification = section("forge"), section("verification")
    notifications = section("notifications")
    continuity = section("continuity")
    return Config(
        hub=HubConfig(
            agent_port=_int(hub, "agent_port"),
            operator_port=_int(hub, "operator_port"),
            main_agent_port=_int(hub, "main_agent_port"),
            operator_bind=cast("str", hub["operator_bind"]),
            main_agent_bind=cast("str", hub["main_agent_bind"]),
            egress_port=_int(hub, "egress_port"),
        ),
        paths=PathsConfig(
            data_dir=cast("str", paths["data_dir"]),
            filter_socket=cast("str", paths["filter_socket"]),
            operator_token_file=cast("str", paths["operator_token_file"]),
            main_agent_token_file=cast("str", paths["main_agent_token_file"]),
            control_socket=cast("str", paths["control_socket"]),
        ),
        runtime=RuntimeConfig(
            core_network=cast("str", runtime["core_network"]),
            hub_container=cast("str", runtime["hub_container"]),
            guard_health_seconds=_int(runtime, "guard_health_seconds"),
            poll_seconds=_int(runtime, "poll_seconds"),
        ),
        images=ImagesConfig(
            instance=cast("str", images["instance"]), guard=cast("str", images["guard"])
        ),
        network=NetworkConfig(
            instance_pool=cast("str", network["instance_pool"]),
            instance_prefix=_int(network, "instance_prefix"),
            resolvers=tuple(cast("list[str]", network["resolvers"])),
            egress=cast("str", network["egress"]),
            allowed_hosts=tuple(cast("list[str]", network["allowed_hosts"])),
        ),
        limits=LimitsConfig(
            cpus=float(cast("float", limits["cpus"])),
            memory_mib=_int(limits, "memory_mib"),
            pids=_int(limits, "pids"),
            guard_memory_mib=_int(limits, "guard_memory_mib"),
            turn_seconds=_int(limits, "turn_seconds"),
            silence_seconds=_int(limits, "silence_seconds"),
            refusals_per_minute=_int(limits, "refusals_per_minute"),
        ),
        tokens=TokensConfig(
            lifetime_seconds=_int(tokens, "lifetime_seconds"),
            rotate_before_seconds=_int(tokens, "rotate_before_seconds"),
            channel_lifetime_days=_int(tokens, "channel_lifetime_days"),
        ),
        gateway=GatewayConfig(
            simulation=_flag(gateway, "simulation"),
            provider=cast("str", gateway["provider"]),
            model=cast("str", gateway["model"]),
            upstream_model=cast("str", gateway.get("upstream_model", gateway["model"])),
            models=tuple(cast("list[str]", gateway.get("models") or [])),
            upstream_url=cast("str | None", gateway.get("upstream_url")),
            credential_file=cast("str | None", gateway.get("credential_file")),
            session_header=cast("str | None", gateway.get("session_header")),
            timeout_seconds=_int(gateway, "timeout_seconds"),
        ),
        forge=ForgeConfig(
            provider=cast("str", forge["provider"]),
            root=cast("str", forge["root"]),
            token_file=cast("str | None", forge.get("token_file")),
        ),
        verification=VerificationConfig(
            runner=cast("str", verification["runner"]),
            kit=cast("str", verification["kit"]),
            timeout_seconds=_int(verification, "timeout_seconds"),
            kit_image=cast("str | None", verification.get("kit_image")),
            protected_paths=tuple(cast("list[str]", verification["protected_paths"])),
        ),
        notifications=NotificationsConfig(
            url=cast("str", notifications["url"]),
            secret=cast("str", notifications["secret"]),
        ),
        continuity=ContinuityConfig(
            inactivity_seconds=_int(continuity, "inactivity_seconds"),
            restarts=_int(continuity, "restarts"),
            unblock_seconds=_int(continuity, "unblock_seconds"),
            rejections=_int(continuity, "rejections"),
        ),
    )
