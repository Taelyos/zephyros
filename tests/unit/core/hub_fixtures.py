"""A hub wired to in-memory services, with a token for every kind of caller."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tests.unit.core.contract_fixtures import seed_operations
from zephyros.adapters.dummy.delivery import InMemoryDeliveryArea
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.contract_store import InMemoryContractStore
from zephyros.adapters.memory.delivery_store import InMemoryDeliveryStore
from zephyros.adapters.memory.instance_control import FakeInstanceControl
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.adapters.memory.project_store import InMemoryProjectStore
from zephyros.adapters.memory.token_store import InMemoryTokenStore
from zephyros.core.audit import AuditLog, AuditRecord
from zephyros.core.continuity import Continuity
from zephyros.core.continuity import Timing as ContinuityTiming
from zephyros.core.contract import ContractService
from zephyros.core.deliveries import DeliveryService
from zephyros.core.hub import Caller, Hub, ToolResponse
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import Principal, instance_principal
from zephyros.core.projects import ProjectService
from zephyros.core.tokens import Endpoint, TokenService
from zephyros.core.webhooks import NotifyResult

ADDRESSES = {
    "owner_a": "10.200.0.2",
    "contractor_a": "10.200.0.18",
    "owner_b": "10.200.0.34",
    "contractor_b": "10.200.0.50",
}


@dataclass
class Env:
    clock: FakeClock
    log_store: InMemoryLogStore
    audit: AuditLog
    tokens: TokenService
    contracts: ContractService
    projects: ProjectService
    instances: FakeInstanceControl
    hub: Hub
    project_a: str
    project_b: str
    callers: dict[str, Caller] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    addresses: dict[str, str] = field(default_factory=dict)
    continuity: Continuity | None = None
    notifications: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    refuse_notifications: list[str] = field(default_factory=list)

    def principal(self, who: str) -> Principal:
        return self.callers[who].principal

    def call(self, who: str, tool: str, **arguments: Any) -> ToolResponse:
        return self.hub.call(self.callers[who], tool, arguments)

    def records(self) -> list[AuditRecord]:
        return [r for stream in self.audit.streams() for r in self.audit.read(stream)]

    def record_count(self) -> int:
        return len(self.records())

    def last(self, stream: str) -> AuditRecord:
        records = self.audit.read(stream)
        return records[-1]


def make_env(*, refusals_per_minute: int | None = None, simulation: bool = False) -> Env:
    clock = FakeClock()
    log_store = InMemoryLogStore()
    audit = AuditLog(log_store, clock)
    tokens = TokenService(InMemoryTokenStore(), clock)
    ids = iter(f"p_{n:010d}" for n in range(1, 100))
    changes = iter(f"c_{n:08d}" for n in range(1, 1000))
    projects = ProjectService(InMemoryProjectStore(), clock, new_id=lambda: next(ids))
    contracts = ContractService(InMemoryContractStore(), clock, new_change_id=lambda: next(changes))
    instances = FakeInstanceControl()
    continuity = Continuity(
        ContinuityTiming(inactivity_seconds=900, restarts=2, unblock_seconds=3600)
    )
    notifications: list[tuple[str, dict[str, object]]] = []
    refuse_notifications: list[str] = []

    def record_notification(event: str, payload: Mapping[str, object]) -> NotifyResult:
        if refuse_notifications:
            return NotifyResult(refused=refuse_notifications[0])
        notifications.append((event, dict(payload)))
        return NotifyResult()

    hub = Hub(
        audit=audit,
        tokens=tokens,
        contracts=contracts,
        projects=projects,
        instances=instances,
        continuity=continuity,
        notify=record_notification,
        clock=clock,
        refusals_per_minute=refusals_per_minute,
        simulation=simulation,
    )
    env = Env(clock, log_store, audit, tokens, contracts, projects, instances, hub, "", "")
    env.continuity = continuity
    env.notifications = notifications
    env.refuse_notifications = refuse_notifications

    operator = tokens.issue_operator_token()
    main = tokens.issue_main_agent_token()
    env.callers["operator"] = _authenticate(hub, operator.secret, "172.18.0.1", Endpoint.OPERATOR)
    env.callers["main"] = _authenticate(hub, main.secret, "172.18.0.1", Endpoint.MAIN_AGENT)
    env.secrets["operator"], env.secrets["main"] = operator.secret, main.secret
    env.addresses["operator"] = env.addresses["main"] = "172.18.0.1"
    env.project_a = str(env.call("operator", "project.create", name="Project A").data["project_id"])  # type: ignore[index]  # reason: data is set on success
    env.project_b = str(env.call("operator", "project.create", name="Project B").data["project_id"])  # type: ignore[index]  # reason: data is set on success
    for project, suffix in ((env.project_a, "a"), (env.project_b, "b")):
        for side, role in ((Side.OWNER, "owner"), (Side.CONTRACTOR, "contractor")):
            principal = instance_principal(side, project)
            address = ADDRESSES[f"{role}_{suffix}"]
            issued = tokens.issue_project_token(principal, f"i_{role}_{suffix}", address, 3600)
            env.callers[f"{role}_{suffix}"] = _authenticate(
                hub, issued.secret, address, Endpoint.AGENT
            )
            env.secrets[f"{role}_{suffix}"] = issued.secret
            env.addresses[f"{role}_{suffix}"] = address
    return env


def _authenticate(hub: Hub, secret: str, address: str, endpoint: Endpoint) -> Caller:
    caller = hub.authenticate(secret, peer_address=address, endpoint=endpoint)
    assert caller is not None
    return caller


def attach_deliveries(env: Env) -> DeliveryService:
    """Give the hub a delivery loop over the same services, for the tools that need one."""
    service = DeliveryService(
        InMemoryDeliveryArea(),
        AssertionRunner(),
        None,
        env.contracts,
        env.projects,
        InMemoryDeliveryStore(),
        env.audit,
        env.clock,
        kit="assertions",
    )
    env.hub._deliveries = service
    return service


def seed_contract(env: Env, project: str | None = None) -> None:
    """Import the seed contract for a project through the operator channel."""
    from tests.unit.core.contract_fixtures import wire

    response = env.call(
        "operator",
        "contract.apply_reserved",
        project_id=project or env.project_a,
        summary="import",
        operations=[wire(o) for o in seed_operations()],
    )
    assert response.ok, response.failure
