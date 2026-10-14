"""The world the attack catalog runs in: a real hub on a SQLite file, two projects with an Owner
and a Contractor instance each, the operator and the main agent.

Instances are emulated by the address their calls come from: the hub binds a token to the
address of its instance, so a call "from" an instance is a call with that peer address. The
network half of the catalog (blocked connections) runs against real containers in the tests
marked `runtime`.

Oracles (threat model, section 5.0):

* O-REFUSE: the call fails with an authorization or network error;
* O-STATE: the protected state is bit-identical before and after (`fingerprint`);
* O-LOG: one record names the instance, the action and the time (`records`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from tests.unit.core.contract_fixtures import seed_operations, wire
from zephyros.adapters.http.agent import build_agent_app
from zephyros.adapters.http.main_agent import build_main_agent_app
from zephyros.adapters.http.operator import build_operator_app
from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.instance_control import FakeInstanceControl
from zephyros.adapters.sqlite.database import Database
from zephyros.app.hub import DeliverySetup, build_hub
from zephyros.core.audit import AuditRecord, canonical_json
from zephyros.core.gateway import CredentialSource, GatewayService
from zephyros.core.hub import Hub
from zephyros.core.ports.agent_engine import Side
from zephyros.core.ports.model_provider import ModelProvider
from zephyros.core.principals import instance_principal
from zephyros.core.tokens import TokenService

OPERATOR_PORT = 8081
HUB_HOST = "172.18.0.1"
MODEL = "zephyros-default"


@dataclass
class Actor:
    """A caller with a token, as seen from a given address."""

    world: World
    name: str
    secret: str
    address: str
    listener: str = "agent"
    """The listener this caller is meant to use: agent, main or operator."""

    def http(self, address: str | None = None, token: str | None = None) -> TestClient:
        app = self.world.main_app if self.listener == "main" else self.world.agent_app
        client = TestClient(app, client=(address or self.address, 40000))
        secret = self.secret if token is None else token
        if secret:
            client.headers["Authorization"] = f"Bearer {secret}"
        return client

    def rpc(self, method: str, params: dict[str, Any] | None = None, **options: Any) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return self.http(**options).post("/mcp", json=body)

    def tool(self, name: str, arguments: dict[str, Any] | None = None, **options: Any) -> Result:
        if self.listener == "operator":
            return self._operator_tool(name, arguments or {})
        response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, **options)
        return Result.of(response)

    def _operator_tool(self, name: str, arguments: dict[str, Any]) -> Result:
        client = self.world.operator_client(token=self.secret)
        response = client.post(
            "/operator/call", json={"tool": name.replace("_", ".", 1), "arguments": arguments}
        )
        if response.status_code == 401:
            return Result(401, {"kind": "http", "reason": "401"}, None)
        body = response.json()
        if body["ok"]:
            return Result(200, None, body["data"])
        error = body["error"]
        return Result(
            response.status_code,
            {
                "error": error["kind"],
                "reason": error.get("reason", ""),
                "message": error.get("message", ""),
            },
            None,
        )


@dataclass
class Result:
    """The outcome of a tool call as an agent sees it."""

    status: int
    error: dict[str, Any] | None
    data: dict[str, Any] | None

    @property
    def refused(self) -> bool:
        return self.status in (401, 403) or self.error is not None

    @staticmethod
    def of(response: Any) -> Result:
        if response.status_code != 200:
            return Result(
                response.status_code, {"kind": "http", "reason": str(response.status_code)}, None
            )
        body = response.json()
        if "error" in body:
            return Result(200, {"kind": "protocol", "reason": body["error"]["message"]}, None)
        content = body["result"]
        payload = json.loads(content["content"][0]["text"])
        if content["isError"]:
            return Result(200, payload, None)
        return Result(200, None, payload)


@dataclass
class World:
    tmp_path: Path
    token_lifetime: int = 3600
    provider: ModelProvider | None = None
    credentials: CredentialSource | None = None
    clock: FakeClock = field(default_factory=FakeClock)
    steps: list[str] = field(default_factory=list)
    instances: FakeInstanceControl = field(default_factory=FakeInstanceControl)
    project_a: str = ""
    project_b: str = ""
    actors: dict[str, Actor] = field(default_factory=dict)
    delivery: DeliverySetup | None = None

    def __post_init__(self) -> None:
        self.db_path = self.tmp_path / "hub.db"
        self._boot()
        self._populate()

    # ------------------------------------------------------------------ boot, restart
    def _boot(self) -> None:
        self.database = Database(self.db_path)
        self.hub: Hub
        self.tokens: TokenService
        self.gateway: GatewayService
        runtime = build_hub(
            self.database,
            self.instances,
            self.clock,
            provider=self.provider,
            model=MODEL,
            credentials=self.credentials,
            delivery=self.delivery,
        )
        self.hub, self.tokens, self.gateway = runtime.hub, runtime.tokens, runtime.gateway
        self.audit = runtime.audit
        self.deliveries = runtime.deliveries
        self.notifier = runtime.notifier
        self.agent_app = build_agent_app(self.hub, model_name=MODEL, gateway=self.gateway)
        self.main_app = build_main_agent_app(self.hub)
        self.operator_app = build_operator_app(self.hub, port=OPERATOR_PORT)

    def run_jobs(self) -> None:
        """Do every queued job of the delivery loop, as its worker would; then send what the
        notifier queued meanwhile, as its own worker would."""
        assert self.deliveries is not None
        while self.deliveries.step():
            pass
        while self.notifier.step():
            pass

    def restart_hub(self) -> None:
        """Stop the hub and start it again on the same database."""
        self.database.close()
        self._boot()

    def _populate(self) -> None:
        operator = self.tokens.issue_operator_token()
        main = self.tokens.issue_main_agent_token()
        self.actors["operator"] = Actor(self, "operator", operator.secret, HUB_HOST, "operator")
        self.actors["main"] = Actor(self, "main", main.secret, HUB_HOST, "main")
        self.project_a = self.operator_tool("project.create", name="Project A")["project_id"]
        self.project_b = self.operator_tool("project.create", name="Project B")["project_id"]
        addresses = iter(f"10.200.0.{16 * n + 2}" for n in range(1, 20))
        for suffix, project in (("a", self.project_a), ("b", self.project_b)):
            for side in (Side.OWNER, Side.CONTRACTOR):
                address = next(addresses)
                principal = instance_principal(side, project)
                issued = self.tokens.issue_project_token(
                    principal, f"i_{side.value}_{suffix}", address, self.token_lifetime
                )
                self.actors[f"{side.value}_{suffix}"] = Actor(
                    self, f"{side.value}_{suffix}", issued.secret, address
                )

    # ------------------------------------------------------------------ actors and channels
    def __getitem__(self, name: str) -> Actor:
        return self.actors[name]

    def operator_client(self, *, token: str | None = None) -> TestClient:
        client = TestClient(
            self.operator_app,
            base_url=f"http://127.0.0.1:{OPERATOR_PORT}",
            client=(HUB_HOST, 44000),
        )
        client.headers["Authorization"] = f"Bearer {token or self.actors['operator'].secret}"
        return client

    def main_client(self, *, token: str | None = None, address: str = HUB_HOST) -> TestClient:
        client = TestClient(self.main_app, client=(address, 45000))
        client.headers["Authorization"] = f"Bearer {token or self.actors['main'].secret}"
        return client

    def operator_tool(self, tool: str, /, **arguments: Any) -> dict[str, Any]:
        response = self.operator_client().post(
            "/operator/call", json={"tool": tool, "arguments": arguments}
        )
        body = response.json()
        assert body["ok"], body
        return body["data"]  # type: ignore[no-any-return]  # reason: JSON body

    def import_contract(self, project: str | None = None) -> None:
        self.operator_tool(
            "contract.apply_reserved",
            project_id=project or self.project_a,
            summary="import",
            operations=[wire(operation) for operation in seed_operations()],
        )

    # ------------------------------------------------------------------ oracles
    def fingerprint(self, project: str) -> str:
        """O-STATE: a hash of every version and every pending change of the contract."""
        store = self.hub._contracts._store  # reason: the oracle looks under the hub
        versions = [store.get(project, n) for n in store.numbers(project)]
        pending = store.pending(project)
        material = {
            "versions": [(v.id, v.content_hash) for v in versions if v is not None],
            "pending": [(c.id, c.status.value) for c in pending],
        }
        return hashlib.sha256(canonical_json(material).encode("ascii")).hexdigest()

    def records(self, stream: str) -> list[AuditRecord]:
        return self.hub._audit.read(stream, 0, 100_000)  # reason: the oracle reads the log

    def verify_logs(self) -> None:
        audit = self.hub._audit  # reason: the oracle reads the log
        for stream in audit.streams():
            report = audit.verify(stream)
            assert report.ok, (stream, report)

    def step(self, label: str) -> None:
        """Mark a step of an attack, ATK-NN.a, for the report."""
        self.steps.append(label)
