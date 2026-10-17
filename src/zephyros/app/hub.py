"""The hub program: read the configuration, build the services, run the listeners.

    python -m zephyros.app.hub --config /etc/zephyros/zephyros.toml

The program refuses to start when the configuration violates the schema, and names each faulty
key, when the operator channel and the main-agent endpoint could listen on an instance network
(ADR 0025), or when the tools directory of the projects could be changed from an instance or
holds a link that leads out of it (ADR 0030). The operator and main-agent tokens are written to
their files the first time (mode 0600), with a lifetime, and rotated on the control socket
(ADR 0026); the hub keeps only their hashes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from zephyros import __version__
from zephyros.adapters.config_file import load_config
from zephyros.adapters.dummy.obedient_stub import ObedientStubProvider
from zephyros.adapters.engines import ENGINES
from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.git.delivery import GitDeliveryArea
from zephyros.adapters.git.forge import DirectoryForge
from zephyros.adapters.github.forge import GitHubForge
from zephyros.adapters.http.agent import build_agent_app
from zephyros.adapters.http.egress_proxy import start_egress_proxy
from zephyros.adapters.http.listen import ListenError, listen_hosts
from zephyros.adapters.http.main_agent import build_main_agent_app
from zephyros.adapters.http.operator import build_operator_app
from zephyros.adapters.http.server import (
    HubServer,
    SocketPathError,
    bind_unix_socket,
    build_server,
    serve_all,
    serve_on,
    stop_all,
)
from zephyros.adapters.project_tools import ToolsDirectory
from zephyros.adapters.providers.openai_compatible import FileCredential, OpenAiCompatibleProvider
from zephyros.adapters.sandbox_runner import SandboxRunner
from zephyros.adapters.sqlite.continuity_store import SqliteContinuityStore
from zephyros.adapters.sqlite.contract_store import SqliteContractStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.adapters.sqlite.instance_store import SqliteInstanceStore
from zephyros.adapters.sqlite.log_store import SqliteLogStore
from zephyros.adapters.sqlite.obstacle_store import SqliteObstacleStore
from zephyros.adapters.sqlite.project_store import SqliteProjectStore
from zephyros.adapters.sqlite.token_store import SqliteTokenStore
from zephyros.adapters.system_clock import SystemClock
from zephyros.adapters.webhooks import Post, WebhookSender
from zephyros.adapters.worker import InstanceWorker
from zephyros.app.channel_tokens import ChannelTokens, build_control_app
from zephyros.core.audit import HUB_STREAM, SYSTEM_PRINCIPAL, Action, AuditEvent, AuditLog, Outcome
from zephyros.core.clock import Clock
from zephyros.core.config import Config, ConfigError, GatewayConfig
from zephyros.core.containers import Policy
from zephyros.core.context import DEFAULT_CONTEXT_WINDOW, context_banner
from zephyros.core.continuity import Continuity
from zephyros.core.continuity import Timing as ContinuityTiming
from zephyros.core.contract import ContractService
from zephyros.core.deliveries import DeliveryService
from zephyros.core.egress import GITHUB_HOST, EgressPolicy, EgressRefusal, host_of
from zephyros.core.gateway import CredentialSource, GatewayLimits, GatewayService
from zephyros.core.hub import Hub
from zephyros.core.instances import InstanceControl
from zephyros.core.lifecycle import InstanceManager, Timing, TokenRenewal, TurnEngine
from zephyros.core.notifications import Notifier, Send
from zephyros.core.obstacles import ObstacleService
from zephyros.core.ports.agent_engine import Side
from zephyros.core.ports.forge import Forge
from zephyros.core.ports.model_provider import ModelProvider
from zephyros.core.ports.verification import INTERFACE_VERSION as VERIFICATION_VERSION
from zephyros.core.ports.verification import (
    VerificationError,
    VerificationRequest,
    VerificationResult,
    VerificationRunner,
)
from zephyros.core.projects import ProjectService
from zephyros.core.tokens import TokenService
from zephyros.core.tools import TOOLS_PATH, ToolsError, ToolsProbe
from zephyros.core.webhooks import EVENTS_SCHEMA_VERSION, NotifyResult

AGENT_BIND = "0.0.0.0"  # noqa: S104  # reason: the agent endpoint is reached on every instance network
CONTROL_MODE = 0o600
"""The control socket is the user of the hub's alone, as are the token files (ADR 0026)."""


InstanceFactory = Callable[
    [TokenService, AuditLog, Clock, Notifier, Continuity | None, ObstacleService], InstanceControl
]
"""Builds the instance control from services the hub creates, which it needs: the obstacles
brief each turn that starts (ADR 0034)."""


@dataclass(frozen=True, slots=True)
class HubRuntime:
    hub: Hub
    tokens: TokenService
    gateway: GatewayService
    audit: AuditLog
    instances: InstanceControl
    notifier: Notifier
    deliveries: DeliveryService | None = None
    continuity: Continuity | None = None
    obstacles: ObstacleService | None = None


def build_forge(config: Config) -> tuple[Forge | None, CredentialSource | None]:
    """The forge of the configuration, and where its credential is read from."""
    if config.forge.provider == "directory":
        return DirectoryForge(Path(config.forge.root)), None
    if config.forge.provider == "github":
        assert config.forge.token_file is not None  # noqa: S101  # reason: the schema requires it
        token = Path(config.forge.token_file)
        return GitHubForge(token), FileCredential(token)
    return None, None


@dataclass(frozen=True, slots=True)
class CombinedCredentials:
    """The secrets of several sources, for the redaction of the journal and the reports."""

    sources: tuple[CredentialSource, ...]

    def secrets(self) -> list[str]:
        return [secret for source in self.sources for secret in source.secrets()]


def build_provider(config: Config) -> tuple[ModelProvider, CredentialSource | None]:
    """The model provider of the configuration, and where its credential is read from."""
    gateway = config.gateway
    if gateway.provider == "openai-compatible":
        assert gateway.upstream_url is not None  # noqa: S101  # reason: the schema requires it
        assert gateway.credential_file is not None  # noqa: S101  # reason: the schema requires it
        credential = FileCredential(Path(gateway.credential_file))
        provider = OpenAiCompatibleProvider(
            gateway.upstream_url,
            credential,
            upstream_model=gateway.upstream_model,
            session_header=gateway.session_header,
            reasoning_echo=gateway.reasoning_echo,
            timeout_seconds=gateway.timeout_seconds,
        )
        return provider, credential
    return ObedientStubProvider(), None


class NoRunner:
    """The runner when none is configured: every verification ends in an error that says so."""

    interface_version = VERIFICATION_VERSION
    kits: frozenset[str] = frozenset()

    def run(self, request: VerificationRequest) -> VerificationResult:
        del request
        raise VerificationError("no verification runner is configured (verification.runner)")


def turn_engines(config: Config) -> dict[Side, TurnEngine]:
    """The agent engine of each side, as each start of a turn records it (ADR 0029). The filter
    reads the same configuration, and holds each side to the image of this engine."""
    policy = Policy.from_config(config)
    return {
        side: TurnEngine(policy.engine(side), ENGINES[policy.engine(side)]().engine_version)
        for side in Side
    }


def build_egress(config: Config) -> EgressPolicy:
    """The hosts the configuration names (EXG-48): the provider, the notification receiver,
    and the accepted repository sources."""
    urls = [config.notifications.url]
    if config.gateway.upstream_url:
        urls.append(config.gateway.upstream_url)
    return EgressPolicy([*[host_of(url) for url in urls if url], GITHUB_HOST])


def build_notifier(config: Config, *, post: Post | None = None) -> Send | None:
    """The signed notifications of the hub (EXG-43) under the egress policy (EXG-48): one signed
    delivery per event, none when the configuration leaves them off. A redirect away from the
    configured hosts is refused, never followed. It sends on the thread that calls it: the hub
    calls it from the notifier's worker only."""
    if not config.notifications.url:
        return None
    policy = build_egress(config)
    sender = WebhookSender(config.notifications.url, config.notifications.secret, post=post)

    def notify(event: str, payload: Mapping[str, object]) -> NotifyResult:
        try:
            policy.check(config.notifications.url, purpose="notification")
        except EgressRefusal as refusal:
            return NotifyResult(refused=refusal.egress.host)
        body = json.dumps(
            {"event": event, "schema_version": EVENTS_SCHEMA_VERSION, **dict(payload)},
            sort_keys=True,
        ).encode()
        answered = sender.deliver("w_" + uuid.uuid4().hex, int(time.time()), body)
        if 300 <= answered.status < 400 and answered.location:
            target = host_of(answered.location)
            if not policy.allows(target):
                return NotifyResult(status=answered.status, refused=target)
        return NotifyResult(status=answered.status)

    return notify


@dataclass(frozen=True, slots=True)
class DeliverySetup:
    """What the delivery loop needs besides the services the hub builds."""

    staging_root: Path
    runner: VerificationRunner
    forge: Forge | None = None
    kit: str = "tests"
    timeout_seconds: int = 300
    protected_paths: tuple[str, ...] = ("CONTRACT.md",)


def build_hub(
    database: Database,
    instances: InstanceControl | None = None,
    clock: Clock | None = None,
    *,
    instance_factory: InstanceFactory | None = None,
    provider: ModelProvider | None = None,
    model: str = "zephyros-default",
    credentials: CredentialSource | None = None,
    delivery: DeliverySetup | None = None,
    continuity: ContinuityTiming | None = None,
    notify: Send | None = None,
    rejections_alert: int | None = None,
    refusals_per_minute: int | None = None,
    simulation: bool = False,
    tools: ToolsProbe | None = None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> HubRuntime:
    if (instances is None) == (instance_factory is None):
        raise ValueError("give either an instance control or a way to build one")
    clock = clock or SystemClock()
    # The anchor of the log head lives beside the database, not inside it (EXG-19, ATK-69): a
    # replacement of the whole file cannot move it. In-memory databases keep none.
    anchor = Path(database.path).parent / "log-anchor.json" if database.path != ":memory:" else None
    audit = AuditLog(SqliteLogStore(database, anchor), clock)
    tokens = TokenService(SqliteTokenStore(database), clock)
    contracts = ContractService(SqliteContractStore(database), clock)
    projects = ProjectService(SqliteProjectStore(database), clock)
    notifier = Notifier(audit, notify)
    obstacles = ObstacleService(SqliteObstacleStore(database), clock)
    # The ladder of each task is kept in the database: a restart takes it back (T12).
    ladders = (
        Continuity(continuity, SqliteContinuityStore(database)) if continuity is not None else None
    )
    if instances is not None:
        control = instances
    else:
        assert instance_factory is not None  # noqa: S101  # reason: checked at the top
        control = instance_factory(tokens, audit, clock, notifier, ladders, obstacles)
    deliveries = None
    hub_box: list[Hub] = []
    known = credentials

    def secrets() -> list[str]:
        return known.secrets() if known is not None else []

    def events(event: str, payload: Mapping[str, object]) -> None:
        if hub_box:
            hub_box[0].notified(event, payload)

    if delivery is not None:
        deliveries = DeliveryService(
            GitDeliveryArea(delivery.staging_root),
            delivery.runner,
            delivery.forge,
            contracts,
            projects,
            SqliteDeliveryStore(database),
            audit,
            clock,
            kit=delivery.kit,
            timeout_seconds=delivery.timeout_seconds,
            protected_paths=delivery.protected_paths,
            version="Zephyros " + __version__,
            secrets=secrets,
            events=events,
            rejections_alert=rejections_alert,
            simulation=simulation,
            tools=tools,
            obstacles=obstacles.of_project,
        )
    hub = Hub(
        audit=audit,
        tokens=tokens,
        contracts=contracts,
        projects=projects,
        instances=control,
        deliveries=deliveries,
        continuity=ladders,
        notifier=notifier,
        clock=clock,
        refusals_per_minute=refusals_per_minute,
        simulation=simulation,
        obstacles=obstacles,
        secrets=secrets,
    )
    hub_box.append(hub)
    hub.check_tasks()
    gateway = GatewayService(
        audit=audit,
        provider=provider or ObedientStubProvider(),
        model=model,
        credentials=credentials,
        limits=GatewayLimits.for_window(context_window),
    )
    return HubRuntime(
        hub, tokens, gateway, audit, control, notifier, deliveries, ladders, obstacles
    )


def record_context(gateway: GatewayConfig, audit: AuditLog) -> None:
    """The context the agents work with, in the log of the hub at every start, and on the
    standard error too what the operator is to know of it (ADR 0033)."""
    context = gateway.context
    warnings = gateway.context_warnings
    audit.append(
        AuditEvent(
            HUB_STREAM,
            SYSTEM_PRINCIPAL,
            Action.CONTEXT_CHECK,
            Outcome.OK,
            parameters={
                "context_window": context.window,
                "context_declared": gateway.context_declared,
                "context_tokens": context.tokens,
                "compaction_tokens": context.compaction,
                "below_floor": context.below_floor,
                **({"warnings": warnings} if warnings else {}),
            },
        )
    )
    if warnings:
        print(context_banner(warnings), file=sys.stderr, flush=True)


async def run(
    config: Config,
    *,
    agent_bind: str = AGENT_BIND,
    stop: asyncio.Event | None = None,
    instances: InstanceControl | None = None,
) -> None:
    """Run the listeners until a signal arrives, or until `stop` is set."""
    # Before anything opens: a hub that cannot keep these two off the instance networks does
    # not start (ADR 0025).
    pool = config.network.instance_pool
    operator_hosts = listen_hosts(config.hub.operator_bind, pool)
    main_agent_hosts = listen_hosts(config.hub.main_agent_bind, pool)
    # Nor does a hub whose tools an instance could change (ADR 0030): measured once here, the
    # fingerprint goes to the log below.
    tools = None if config.tools.source is None else ToolsDirectory(Path(config.tools.source))
    measured = None if tools is None else tools.digest()
    control = bind_unix_socket(Path(config.paths.control_socket), CONTROL_MODE)
    data_dir = Path(config.paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "hub.db")
    provider, credentials = build_provider(config)
    notify = build_notifier(config)
    broker: FilterClient | None = None
    factory: InstanceFactory | None = None
    if instances is None:
        broker = FilterClient.over_socket(config.paths.filter_socket)
        filter_client = broker

        def factory(
            tokens: TokenService,
            audit: AuditLog,
            clock: Clock,
            notifier: Notifier,
            continuity: Continuity | None,
            obstacles: ObstacleService,
        ) -> InstanceControl:
            return InstanceManager(
                filter_client,
                SqliteInstanceStore(database),
                tokens,
                audit,
                clock,
                Timing.from_config(config),
                continuity=continuity,
                notifier=notifier,
                engines=turn_engines(config),
                tools=tools,
                briefing=obstacles,
            )

    forge, forge_credentials = build_forge(config)
    if forge_credentials is not None:
        credentials = (
            CombinedCredentials((credentials, forge_credentials))
            if credentials is not None
            else forge_credentials
        )
    runtime = build_hub(
        database,
        instances,
        instance_factory=factory,
        delivery=DeliverySetup(
            staging_root=data_dir / "staging",
            runner=(
                SandboxRunner(
                    broker or FilterClient.over_socket(config.paths.filter_socket),
                    [config.verification.kit],
                )
                if config.verification.runner == "sandbox"
                else NoRunner()
            ),
            forge=forge,
            kit=config.verification.kit,
            timeout_seconds=config.verification.timeout_seconds,
            protected_paths=config.verification.protected_paths,
        ),
        provider=provider,
        model=config.gateway.model,
        credentials=credentials,
        continuity=ContinuityTiming(
            inactivity_seconds=config.continuity.inactivity_seconds,
            restarts=config.continuity.restarts,
            unblock_seconds=config.continuity.unblock_seconds,
        ),
        notify=notify,
        rejections_alert=config.continuity.rejections,
        refusals_per_minute=config.limits.refusals_per_minute,
        simulation=config.gateway.simulation,
        tools=tools,
        context_window=config.gateway.context.window,
    )
    hub = runtime.hub
    if tools is not None and measured is not None:
        runtime.audit.append(
            AuditEvent(
                HUB_STREAM,
                SYSTEM_PRINCIPAL,
                Action.TOOLS_CHECK,
                Outcome.OK,
                parameters={
                    "source": tools.root,
                    "target": TOOLS_PATH,
                    "tools": measured.fingerprint,
                    "files": measured.files,
                    "bytes": measured.bytes,
                },
            )
        )
    record_context(config.gateway, runtime.audit)
    channel_tokens = ChannelTokens.from_config(config, runtime.tokens, runtime.audit)
    channel_tokens.prepare()
    workers: list[InstanceWorker] = []
    if isinstance(runtime.instances, InstanceManager):
        workers.append(InstanceWorker(runtime.instances, config.runtime.poll_seconds))
        # The tokens of the turns are renewed on a thread of their own: a start that waits for
        # a guard, or a round that waits for the filter, holds up no renewal (ADR 0016, amended).
        workers.append(InstanceWorker(TokenRenewal(runtime.instances), config.runtime.poll_seconds))
    if runtime.deliveries is not None:
        # The queue of the delivery loop is in memory: a verification that waited or ran when the
        # hub stopped runs again from the start.
        runtime.deliveries.resume()
        workers.append(InstanceWorker(runtime.deliveries, config.runtime.poll_seconds))
    # The notifications go out on a thread of their own, never on a listener's loop nor on a
    # worker above; stopped last, so that it records what the others raised on their way out.
    workers.append(InstanceWorker(runtime.notifier, config.runtime.poll_seconds))
    for worker in workers:
        worker.start()
    servers = [
        build_server(
            build_agent_app(hub, model_name=config.gateway.model, gateway=runtime.gateway),
            agent_bind,
            config.hub.agent_port,
        ),
        build_server(
            build_main_agent_app(hub, instance_pool=pool),
            main_agent_hosts,
            config.hub.main_agent_port,
        ),
        build_server(
            build_operator_app(hub, port=config.hub.operator_port, instance_pool=pool),
            operator_hosts,
            config.hub.operator_port,
        ),
        serve_on(build_control_app(channel_tokens), control),
    ]
    loop = asyncio.get_running_loop()
    egress: asyncio.Server | None = None
    waiter: asyncio.Task[None] | None = None
    try:
        # Inside the try: a port already taken stops the hub with everything closed (ADR 0024).
        egress = await start_egress_proxy(config, hub, runtime.audit, agent_bind)
        if stop is None:
            for number in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(number, stop_all, servers)
        else:
            waiter = loop.create_task(_stop_when_set(stop, servers))
        await serve_all(servers)
    finally:
        if waiter is not None:
            waiter.cancel()
        if egress is not None:
            egress.close()
        for worker in workers:
            worker.stop()
        if broker is not None:
            broker.close()
        await provider.aclose()
        database.close()
        Path(config.paths.control_socket).unlink(missing_ok=True)


async def _stop_when_set(stop: asyncio.Event, servers: list[HubServer]) -> None:
    await stop.wait()
    stop_all(servers)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zephyros-hub")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"refused to start: {error}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(config))
    except (ListenError, SocketPathError, ToolsError) as error:
        print(f"refused to start: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
