"""The hub program: read the configuration, build the services, run the three listeners.

    python -m zephyros.app.hub --config /etc/zephyros/zephyros.toml

The program refuses to start when the configuration violates the schema, and names each faulty
key. The operator and main-agent tokens are written to their files the first time (mode 0600);
the hub keeps only their hashes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.git.delivery import GitDeliveryArea
from zephyros.adapters.git.forge import DirectoryForge
from zephyros.adapters.github.forge import GitHubForge
from zephyros.adapters.http.agent import build_agent_app
from zephyros.adapters.http.main_agent import build_main_agent_app
from zephyros.adapters.http.operator import build_operator_app
from zephyros.adapters.http.server import HubServer, build_server, serve_all, stop_all
from zephyros.adapters.providers.openai_compatible import FileCredential, OpenAiCompatibleProvider
from zephyros.adapters.sandbox_runner import SandboxRunner
from zephyros.adapters.sqlite.contract_store import SqliteContractStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.adapters.sqlite.instance_store import SqliteInstanceStore
from zephyros.adapters.sqlite.log_store import SqliteLogStore
from zephyros.adapters.sqlite.project_store import SqliteProjectStore
from zephyros.adapters.sqlite.token_store import SqliteTokenStore
from zephyros.adapters.system_clock import SystemClock
from zephyros.adapters.webhooks import Post, WebhookSender
from zephyros.adapters.worker import InstanceWorker
from zephyros.core.audit import AuditLog
from zephyros.core.clock import Clock
from zephyros.core.config import Config, ConfigError
from zephyros.core.continuity import Continuity
from zephyros.core.continuity import Timing as ContinuityTiming
from zephyros.core.contract import ContractService
from zephyros.core.deliveries import DeliveryService
from zephyros.core.egress import GITHUB_HOST, EgressPolicy, EgressRefusal, host_of
from zephyros.core.gateway import CredentialSource, GatewayService
from zephyros.core.hub import Hub
from zephyros.core.instances import InstanceControl
from zephyros.core.lifecycle import InstanceManager, Timing
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
from zephyros.core.tokens import Endpoint, TokenService
from zephyros.core.webhooks import EVENTS_SCHEMA_VERSION, NotifyResult

AGENT_BIND = "0.0.0.0"  # noqa: S104  # reason: the agent endpoint is reached on every instance network


def ensure_token_file(path: Path, issue: str | None, working: bool) -> None:
    """Write `issue` to `path` with mode 0600 unless a working token is already there."""
    if working or issue is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(issue + "\n")
    path.chmod(0o600)


def prepare_tokens(config: Config, tokens: TokenService, hub_probe: Hub) -> None:
    """Make sure the operator and the main agent each have a working token in its file."""
    for path_text, endpoint, issuer in (
        (config.paths.operator_token_file, Endpoint.OPERATOR, tokens.issue_operator_token),
        (config.paths.main_agent_token_file, Endpoint.MAIN_AGENT, tokens.issue_main_agent_token),
    ):
        path = Path(path_text)
        current = path.read_text(encoding="ascii").strip() if path.exists() else None
        working = (
            current is not None
            and hub_probe.authenticate(current, peer_address="127.0.0.1", endpoint=endpoint)
            is not None
        )
        ensure_token_file(path, None if working else issuer().secret, working)


InstanceFactory = Callable[[TokenService, AuditLog, Clock], InstanceControl]
"""Builds the instance control from services the hub creates, which it needs."""


@dataclass(frozen=True, slots=True)
class HubRuntime:
    hub: Hub
    tokens: TokenService
    gateway: GatewayService
    audit: AuditLog
    instances: InstanceControl
    deliveries: DeliveryService | None = None


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


def build_egress(config: Config) -> EgressPolicy:
    """The hosts the configuration names (EXG-48): the provider, the notification receiver,
    and the accepted repository sources."""
    urls = [config.notifications.url]
    if config.gateway.upstream_url:
        urls.append(config.gateway.upstream_url)
    return EgressPolicy([*[host_of(url) for url in urls if url], GITHUB_HOST])


def build_notifier(
    config: Config, *, post: Post | None = None
) -> Callable[[str, Mapping[str, object]], NotifyResult] | None:
    """The signed notifications of the hub (EXG-43) under the egress policy (EXG-48): one signed
    delivery per event, none when the configuration leaves them off. A redirect away from the
    configured hosts is refused, never followed."""
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
    continuity: Continuity | None = None,
    notify: Callable[[str, Mapping[str, object]], NotifyResult] | None = None,
    rejections_alert: int | None = None,
    refusals_per_minute: int | None = None,
    simulation: bool = False,
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
    if instances is not None:
        control = instances
    else:
        assert instance_factory is not None  # noqa: S101  # reason: checked at the top
        control = instance_factory(tokens, audit, clock)
    deliveries = None
    hub_box: list[Hub] = []

    def events(event: str, payload: Mapping[str, object]) -> None:
        if hub_box:
            hub_box[0].notified(event, payload)

    if delivery is not None:
        known = credentials
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
            version="Zephyros " + __version__,
            secrets=lambda: known.secrets() if known is not None else [],
            events=events,
            rejections_alert=rejections_alert,
            simulation=simulation,
        )
    hub = Hub(
        audit=audit,
        tokens=tokens,
        contracts=contracts,
        projects=projects,
        instances=control,
        deliveries=deliveries,
        continuity=continuity,
        notify=notify,
        clock=clock,
        refusals_per_minute=refusals_per_minute,
        simulation=simulation,
    )
    hub_box.append(hub)
    gateway = GatewayService(
        audit=audit,
        provider=provider or ObedientStubProvider(),
        model=model,
        credentials=credentials,
    )
    return HubRuntime(hub, tokens, gateway, audit, control, deliveries)


async def run(
    config: Config,
    *,
    agent_bind: str = AGENT_BIND,
    stop: asyncio.Event | None = None,
    instances: InstanceControl | None = None,
) -> None:
    """Run the three listeners until a signal arrives, or until `stop` is set."""
    data_dir = Path(config.paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "hub.db")
    provider, credentials = build_provider(config)
    continuity = Continuity(
        ContinuityTiming(
            inactivity_seconds=config.continuity.inactivity_seconds,
            restarts=config.continuity.restarts,
            unblock_seconds=config.continuity.unblock_seconds,
        )
    )
    notify = build_notifier(config)
    broker: FilterClient | None = None
    factory: InstanceFactory | None = None
    if instances is None:
        broker = FilterClient.over_socket(config.paths.filter_socket)
        filter_client = broker

        def factory(tokens: TokenService, audit: AuditLog, clock: Clock) -> InstanceControl:
            return InstanceManager(
                filter_client,
                SqliteInstanceStore(database),
                tokens,
                audit,
                clock,
                Timing.from_config(config),
                continuity=continuity,
                notify=notify,
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
        ),
        provider=provider,
        model=config.gateway.model,
        credentials=credentials,
        continuity=continuity,
        notify=notify,
        rejections_alert=config.continuity.rejections,
        refusals_per_minute=config.limits.refusals_per_minute,
        simulation=config.gateway.simulation,
    )
    hub = runtime.hub
    prepare_tokens(config, runtime.tokens, hub)
    workers: list[InstanceWorker] = []
    if isinstance(runtime.instances, InstanceManager):
        workers.append(InstanceWorker(runtime.instances, config.runtime.poll_seconds))
    if runtime.deliveries is not None:
        workers.append(InstanceWorker(runtime.deliveries, config.runtime.poll_seconds))
    for worker in workers:
        worker.start()
    servers = [
        build_server(
            build_agent_app(hub, model_name=config.gateway.model, gateway=runtime.gateway),
            agent_bind,
            config.hub.agent_port,
        ),
        build_server(
            build_main_agent_app(hub), config.hub.main_agent_bind, config.hub.main_agent_port
        ),
        build_server(
            build_operator_app(hub, port=config.hub.operator_port),
            config.hub.operator_bind,
            config.hub.operator_port,
        ),
    ]
    loop = asyncio.get_running_loop()
    if stop is None:
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(number, stop_all, servers)
    else:
        waiter = loop.create_task(_stop_when_set(stop, servers))
    try:
        await serve_all(servers)
    finally:
        if stop is not None:
            waiter.cancel()
        for worker in workers:
            worker.stop()
        if broker is not None:
            broker.close()
        await provider.aclose()
        database.close()


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
    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
