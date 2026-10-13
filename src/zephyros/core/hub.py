"""The hub: authenticate, authorize, execute and log every call (US-E1-07, US-E1-11, US-E1-12).

Every call, from an instance, the main agent or the operator, goes through `Hub.call`:

1. the tool must exist in the registry, else the call is refused as unknown;
2. `decide` must allow the principal to do the action on the project of the call, where the
   project is the one the call names or, for an instance, its own;
3. the arguments must fit the tool's closed set of fields;
4. the handler runs.

Exactly one record is written to the log for each call, whatever happens. Identity comes from
the token record and the connection, never from a field of the request.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from zephyros.core.audit import (
    HUB_STREAM,
    Action,
    AuditEvent,
    AuditLog,
    Outcome,
    Reason,
    format_line,
)
from zephyros.core.clock import Clock, SystemClock
from zephyros.core.continuity import Continuity, TaskState
from zephyros.core.contract import (
    Change,
    ContractError,
    ContractService,
    Operation,
    parse_change,
    validate_object,
)
from zephyros.core.deliveries import DeliveryRecord, DeliveryService
from zephyros.core.errors import Refusal, remedy
from zephyros.core.importer import import_markdown
from zephyros.core.instances import MAX_PROMPT, InstanceControl
from zephyros.core.limits import RefusalBudget
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import Principal
from zephyros.core.projects import CREATE_INPUT, ProjectService
from zephyros.core.rights import decide
from zephyros.core.schema import Array, Choice, Field, Free, Integer, Problem, Table, Text
from zephyros.core.sources import REFUSAL as SOURCE_REFUSAL
from zephyros.core.sources import accepted as source_accepted
from zephyros.core.tokens import Authenticated, Endpoint, Rejected, TokenService
from zephyros.core.webhooks import NotifyResult
from zephyros.core.writing import forbidden_terms_in

SYSTEM_PRINCIPAL = "hub"


def _instance_side(principal: Principal) -> Side | None:
    """The side of an instance principal, or None for the operator and the main agent."""
    prefix = principal.id.split(":", 1)[0]
    for side in (Side.OWNER, Side.CONTRACTOR):
        if prefix == side.value:
            return side
    return None


UNAUTHENTICATED: Final = "unauthenticated"


@dataclass(frozen=True, slots=True)
class Caller:
    """Who calls, from where. Built from an authenticated token and the connection."""

    principal: Principal
    token_id: str
    source: str
    endpoint: Endpoint


@dataclass(frozen=True, slots=True)
class ToolFailure:
    kind: str
    """forbidden, invalid, not_found, conflict, unknown_tool or internal."""
    reason: Reason
    message: str
    action: str
    """The documented corrective action (EXG-51)."""


@dataclass(frozen=True, slots=True)
class ToolResponse:
    ok: bool
    data: Mapping[str, object] | None = None
    failure: ToolFailure | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a handler returns: the data for the caller, and what the log keeps."""

    data: dict[str, object]
    audit: dict[str, object]
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    action: Action
    description: str
    inputs: Table
    scoped: bool = False
    """Whether the tool acts on a project, named by `project_id` or, for an instance, its own."""

    @property
    def mcp_name(self) -> str:
        return self.name.replace(".", "_")


Handler = Callable[[Caller, Mapping[str, object]], ToolResult]

_PROJECT = Field(
    "project_id", Text(pattern=r"p_[a-z0-9]{6,32}"), "The project; an instance's own by default"
)
_SIDE = Field("side", Choice(("owner", "contractor")), "The side of the project", required=True)
_SUMMARY = Field(
    "summary",
    Text(min_length=1, max_length=300, multiline=True),
    "Why the change is made",
    required=True,
)
_OPERATIONS = Field(
    "operations",
    Array(Free(), min_items=1, max_items=200),
    "The operations of the change",
    required=True,
)
_CHANGE = Field("change_id", Text(pattern=r"c_[a-z0-9]{4,16}"), "The pending change", required=True)


def _spec(
    name: str, action: Action, description: str, *fields: Field, scoped: bool = False
) -> ToolSpec:
    return ToolSpec(name, action, description, Table(fields), scoped)


TOOL_SPECS: Mapping[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        _spec(
            "whoami",
            Action.WHOAMI,
            "Say who the hub takes the caller to be. A Contractor instance also reaches, with "
            "its project token: the model gateway at `/v1`, and the staging area of its "
            "project — `GET /staging/base` (the base to start from, as a git bundle) and "
            "`POST /staging/delivery?branch=<name>` (its work, as a git bundle).",
        ),
        ToolSpec(
            "project.create",
            Action.PROJECT_CREATE,
            "Create a project from a name and, as inert text, a repository URL.",
            CREATE_INPUT,
        ),
        _spec("project.list", Action.PROJECT_LIST, "List the projects the caller may see."),
        _spec("project.get", Action.PROJECT_GET, "Describe one project.", _PROJECT, scoped=True),
        _spec(
            "instance.start",
            Action.INSTANCE_START,
            "Start the instance of a side of a project, with the first prompt of a turn.",
            _PROJECT,
            _SIDE,
            Field(
                "prompt",
                Text(min_length=1, max_length=MAX_PROMPT, multiline=True),
                "The first prompt of the turn",
                required=True,
            ),
            scoped=True,
        ),
        _spec(
            "instance.stop",
            Action.INSTANCE_STOP,
            "Stop the instance of a side.",
            _PROJECT,
            _SIDE,
            scoped=True,
        ),
        _spec(
            "instance.status",
            Action.INSTANCE_STATUS,
            "Describe the instance of a side.",
            _PROJECT,
            _SIDE,
            scoped=True,
        ),
        _spec(
            "contract.read",
            Action.CONTRACT_READ,
            "Read a version of the contract (the current one by default) as data.",
            _PROJECT,
            Field("version", Integer(0, 1_000_000), "A version number"),
            scoped=True,
        ),
        _spec(
            "contract.propose",
            Action.CONTRACT_PROPOSE,
            "Propose a change to the contract. Another principal approves or rejects it.",
            _PROJECT,
            _SUMMARY,
            _OPERATIONS,
            scoped=True,
        ),
        _spec(
            "contract.approve",
            Action.CONTRACT_APPROVE,
            "Approve a pending change of another principal: it becomes a new version.",
            _PROJECT,
            _CHANGE,
            scoped=True,
        ),
        _spec(
            "contract.reject",
            Action.CONTRACT_REJECT,
            "Reject a pending change: the current version stays.",
            _PROJECT,
            _CHANGE,
            scoped=True,
        ),
        _spec(
            "contract.apply_reserved",
            Action.CONTRACT_APPLY_RESERVED,
            "Apply a change to operator-reserved data. The operator only.",
            Field("project_id", Text(pattern=r"p_[a-z0-9]{6,32}"), "The project", required=True),
            _SUMMARY,
            _OPERATIONS,
            scoped=True,
        ),
        _spec(
            "contract.import",
            Action.CONTRACT_IMPORT,
            "Import the Markdown document of the project as contract objects: requirement and "
            "constraint tables, story sections with their criteria and links. The operator only.",
            Field("project_id", Text(pattern=r"p_[a-z0-9]{6,32}"), "The project", required=True),
            _SUMMARY,
            Field(
                "markdown",
                Text(min_length=1, max_length=200_000, multiline=True),
                "The Markdown document of the project",
                required=True,
            ),
            scoped=True,
        ),
        _spec(
            "project.prepare",
            Action.PROJECT_PREPARE,
            "Take the base of the project's staging area from a branch of its repository on the "
            "forge.",
            Field("project_id", Text(pattern=r"p_[a-z0-9]{6,32}"), "The project", required=True),
            Field(
                "repository",
                Text(
                    max_length=201,
                    pattern=r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}",
                ),
                "The repository on the forge, as owner/name",
                required=True,
            ),
            Field(
                "branch",
                Text(max_length=100, pattern=r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}"),
                "The branch the deliveries start from",
                required=True,
            ),
            scoped=True,
        ),
        _spec(
            "acceptance.put",
            Action.ACCEPTANCE_PUT,
            "Keep an acceptance test of the Owner side. Every verification uses it, whatever a "
            "delivery contains.",
            _PROJECT,
            Field(
                "path",
                Text(max_length=151, pattern=r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}"),
                "Where it goes in the tree",
                required=True,
            ),
            Field("content", Text(max_length=65536, multiline=True), "The file", required=True),
            scoped=True,
        ),
        _spec(
            "delivery.declare",
            Action.DELIVERY_DECLARE,
            "Declare the delivery of the branch pushed to `POST /staging/delivery?branch=<name>`: "
            "the hub replays the acceptance tests, computes the gate and decides. The Contractor "
            "only.",
            _PROJECT,
            Field("delivery_id", Text(pattern=r"d[0-9]{1,6}"), "The delivery", required=True),
            scoped=True,
        ),
        _spec(
            "delivery.status",
            Action.DELIVERY_STATUS,
            "Describe a delivery: its state, the decision, the evidence report, the pull request.",
            _PROJECT,
            Field("delivery_id", Text(pattern=r"d[0-9]{1,6}"), "The delivery", required=True),
            scoped=True,
        ),
        _spec(
            "log.read",
            Action.LOG_READ,
            "Read records of the log. The operator only.",
            Field(
                "stream",
                Text(pattern=r"[a-z0-9][a-z0-9_-]{0,63}"),
                "A project or 'hub'",
                required=True,
            ),
            Field("after", Integer(0, 2**40), "Records after this sequence number"),
            Field("limit", Integer(1, 500), "At most this many records"),
        ),
        _spec(
            "log.verify",
            Action.LOG_VERIFY,
            "Verify the chain of a stream. The operator only.",
            Field(
                "stream",
                Text(pattern=r"[a-z0-9][a-z0-9_-]{0,63}"),
                "A project or 'hub'",
                required=True,
            ),
        ),
    )
}

_KIND = {
    Reason.FORBIDDEN: "forbidden",
    Reason.OTHER_PROJECT: "forbidden",
    Reason.SELF_APPROVAL: "forbidden",
    Reason.RESERVED_DATA: "forbidden",
    Reason.INVALID_REQUEST: "invalid",
    Reason.NOT_FOUND: "not_found",
    Reason.NOT_PENDING: "conflict",
    Reason.STALE_BASE: "conflict",
    Reason.UNKNOWN_ACTION: "unknown_tool",
}


def _problems_text(problems: list[Problem]) -> str:
    return "; ".join(str(problem) for problem in problems)


def _flagged_terms(change: Change) -> dict[str, list[str]]:
    """EXG-29: the requirements and criteria of a change whose text holds a forbidden term.
    A flag, never a refusal: the Owner decides what to do with it."""
    flagged: dict[str, list[str]] = {}
    for operation in change.operations:
        item = operation.object or {}
        if operation.op != "put" or item.get("type") not in ("requirement", "criterion"):
            continue
        found = forbidden_terms_in(str(item.get("text", "")))
        if found:
            flagged[str(item.get("id", "?"))] = list(found)
    return flagged


class Hub:
    """The services of the hub behind one entry point."""

    def __init__(
        self,
        *,
        audit: AuditLog,
        tokens: TokenService,
        contracts: ContractService,
        projects: ProjectService,
        instances: InstanceControl,
        deliveries: DeliveryService | None = None,
        continuity: Continuity | None = None,
        notify: Callable[[str, Mapping[str, object]], NotifyResult] | None = None,
        clock: Clock | None = None,
        refusals_per_minute: int | None = None,
        simulation: bool = False,
    ) -> None:
        self._deliveries = deliveries
        self._audit = audit
        self._tokens = tokens
        self._contracts = contracts
        self._projects = projects
        self._instances = instances
        self._continuity = continuity
        self._notify = notify or (lambda _event, _payload: NotifyResult())
        self._clock = clock or SystemClock()
        self._budget = RefusalBudget(refusals_per_minute) if refusals_per_minute else None
        self._simulation = simulation
        self._handlers: dict[str, Handler] = {
            "whoami": self._whoami,
            "project.create": self._project_create,
            "project.list": self._project_list,
            "project.get": self._project_get,
            "instance.start": self._instance_start,
            "instance.stop": self._instance_stop,
            "instance.status": self._instance_status,
            "contract.read": self._contract_read,
            "contract.propose": self._contract_propose,
            "contract.approve": self._contract_approve,
            "contract.reject": self._contract_reject,
            "contract.apply_reserved": self._contract_apply_reserved,
            "contract.import": self._contract_import,
            "log.read": self._log_read,
            "log.verify": self._log_verify,
            "project.prepare": self._project_prepare,
            "acceptance.put": self._acceptance_put,
            "delivery.declare": self._delivery_declare,
            "delivery.status": self._delivery_status,
        }
        if set(self._handlers) != set(TOOL_SPECS):
            raise RuntimeError("the registry of tools and the specifications differ")

    # ------------------------------------------------------------------ authentication
    def authenticate(
        self, secret: str | None, *, peer_address: str | None, endpoint: Endpoint
    ) -> Caller | None:
        """Identify the caller. A refusal is logged once and returns None."""
        result = self._tokens.authenticate(secret, peer_address=peer_address, endpoint=endpoint)
        if isinstance(result, Authenticated):
            return Caller(result.principal, result.record.token_id, peer_address or "", endpoint)
        self._log_authentication_refusal(result, peer_address, endpoint)
        return None

    def _log_authentication_refusal(
        self, rejected: Rejected, peer_address: str | None, endpoint: Endpoint
    ) -> None:
        principal = rejected.principal
        self._audit.append(
            AuditEvent(
                stream=principal.project_id if principal and principal.project_id else HUB_STREAM,
                principal=principal.id if principal else UNAUTHENTICATED,
                action=Action.AUTHENTICATION,
                outcome=Outcome.REFUSED,
                reason=rejected.reason,
                parameters={
                    "source": peer_address or "",
                    "endpoint": endpoint.value,
                    "presented": rejected.presented,
                    "token_id": rejected.token_id,
                },
            )
        )

    def log_refusal(
        self,
        principal_id: str,
        stream: str,
        action: Action,
        reason: Reason,
        parameters: Mapping[str, object],
    ) -> None:
        """Record a refusal by a transport (a browser request, a bad Host header)."""
        self._audit.append(
            AuditEvent(stream, principal_id, action, Outcome.REFUSED, reason, parameters)
        )

    def log_caller_refusal(self, caller: Caller, reason: Reason, detail: str) -> None:
        """Record a refusal of an authenticated caller by a transport (a body that is not JSON)."""
        self._audit.append(
            AuditEvent(
                self._stream(caller, None),
                caller.principal.id,
                Action.UNKNOWN,
                Outcome.REFUSED,
                reason,
                {"source": caller.source, "detail": detail},
            )
        )

    # ------------------------------------------------------------------ the registry
    def tools_for(self, caller: Caller) -> list[ToolSpec]:
        """The tools the caller may use: what `tools/list` shows."""
        return [
            spec
            for spec in TOOL_SPECS.values()
            if decide(caller.principal, spec.action, caller.principal.project_id).allowed
        ]

    def find(self, name: str) -> ToolSpec | None:
        """A tool by its name or by its name in MCP (dots written as underscores)."""
        if name in TOOL_SPECS:
            return TOOL_SPECS[name]
        for spec in TOOL_SPECS.values():
            if spec.mcp_name == name:
                return spec
        return None

    # ------------------------------------------------------------------ the call
    def call(self, caller: Caller, name: object, arguments: object) -> ToolResponse:
        principal = caller.principal
        if arguments is None:
            arguments = {}
        if (
            self._budget is not None
            and _instance_side(principal) is not None
            and self._budget.over(principal.id, time.monotonic())
        ):
            # ATK-71: the instance flooded the log with refusals; answer without the work —
            # whatever it asks for — and record this refusal too.
            return self._fail(
                caller,
                Action.UNKNOWN,
                self._stream(caller, None),
                Reason.RATE_LIMITED,
                "too many refused actions from this instance",
                {},
            )
        spec = self.find(name) if isinstance(name, str) else None
        if spec is None:
            return self._fail(
                caller,
                Action.UNKNOWN,
                self._stream(caller, None),
                Reason.UNKNOWN_ACTION,
                "there is no such tool",
                {"requested": name if isinstance(name, str) else repr(name)},
            )
        raw_project = arguments.get("project_id") if isinstance(arguments, Mapping) else None
        project_id = raw_project if isinstance(raw_project, str) else None
        if spec.scoped and project_id is None:
            project_id = principal.project_id
        decision = decide(principal, spec.action, project_id if spec.scoped else None)
        if not decision.allowed:
            return self._fail(
                caller,
                spec.action,
                self._stream(caller, project_id),
                decision.reason,
                f"a {principal.kind.value} may not use {spec.name} here",
                {"project_id": project_id},
            )
        problems = spec.inputs.check(arguments, "arguments")
        if problems:
            return self._fail(
                caller,
                spec.action,
                self._stream(caller, project_id),
                Reason.INVALID_REQUEST,
                _problems_text(problems),
                {"project_id": project_id},
            )
        assert isinstance(arguments, Mapping)  # noqa: S101  # reason: checked by the table above
        self._observe(caller)
        try:
            result = self._handlers[spec.name](caller, arguments)
        except Refusal as refusal:
            return self._fail(
                caller,
                spec.action,
                self._stream(caller, project_id),
                refusal.reason,
                str(refusal),
                {"project_id": project_id},
            )
        except ContractError as error:
            return self._fail(
                caller,
                spec.action,
                self._stream(caller, project_id),
                Reason.INVALID_REQUEST,
                str(error),
                {"project_id": project_id},
            )
        self._audit.append(
            AuditEvent(
                stream=self._stream(caller, result.project_id or project_id),
                principal=principal.id,
                action=spec.action,
                outcome=Outcome.OK,
                parameters={"source": caller.source, **result.audit},
            )
        )
        return ToolResponse(True, data=result.data)

    def _stream(self, caller: Caller, project_id: str | None) -> str:
        if caller.principal.project_id is not None:
            return caller.principal.project_id
        if project_id is not None and self._projects_exist(project_id):
            return project_id
        return HUB_STREAM

    def _projects_exist(self, project_id: str) -> bool:
        try:
            self._projects.get(project_id)
        except Refusal:
            return False
        return True

    def _observe(self, caller: Caller) -> None:
        """A call of an instance is activity on its side's task (US-E7-01)."""
        if self._continuity is None or caller.principal.project_id is None:
            return
        side = caller.principal.id.split(":", 1)[0]
        if side in (Side.OWNER.value, Side.CONTRACTOR.value):
            self._continuity.observe(caller.principal.project_id, side, self._clock.now())

    def notified(self, event: str, payload: Mapping[str, object]) -> None:
        """The events of the delivery loop reach the continuity and the main agent."""
        if event == "staging.prepared" and self._continuity is not None:
            self._continuity.expect(
                TaskState(
                    str(payload["project_id"]),
                    Side.CONTRACTOR.value,
                    "delivery",
                    "the base of "
                    f"{payload.get('repository', '?')} ({payload.get('branch', '?')}) is ready "
                    "and awaits a delivery",
                    self._clock.now(),
                )
            )
        if event in ("delivery.decided", "delivery.loop"):
            marked = {**dict(payload), "simulation": self._simulation}
            result = self._notify(event, marked)
            project = str(payload.get("project_id", "")) or "hub"
            parameters = {"event": event, **marked}
            if result.refused:
                self._audit.append(
                    AuditEvent(
                        stream=project,
                        principal=SYSTEM_PRINCIPAL,
                        action=Action.EGRESS,
                        outcome=Outcome.REFUSED,
                        reason=Reason.EGRESS,
                        parameters={
                            "host": result.refused,
                            "purpose": "notification",
                        },
                    )
                )
                self._audit.append(
                    AuditEvent(
                        stream=project,
                        principal=SYSTEM_PRINCIPAL,
                        action=Action.NOTIFICATION,
                        outcome=Outcome.ERROR,
                        parameters={**parameters, "error": Reason.EGRESS.value},
                    )
                )
            elif result.status is not None and not 200 <= result.status < 300:
                self._audit.append(
                    AuditEvent(
                        stream=project,
                        principal=SYSTEM_PRINCIPAL,
                        action=Action.NOTIFICATION,
                        outcome=Outcome.ERROR,
                        parameters={**parameters, "error": f"status {result.status}"},
                    )
                )
            else:
                self._audit.append(
                    AuditEvent(
                        stream=project,
                        principal=SYSTEM_PRINCIPAL,
                        action=Action.NOTIFICATION,
                        outcome=Outcome.OK,
                        parameters=parameters,
                    )
                )

    def _fail(
        self,
        caller: Caller,
        action: Action,
        stream: str,
        reason: Reason,
        message: str,
        parameters: Mapping[str, object],
    ) -> ToolResponse:
        self._audit.append(
            AuditEvent(
                stream=stream,
                principal=caller.principal.id,
                action=action,
                outcome=Outcome.REFUSED,
                reason=reason,
                parameters={"source": caller.source, "detail": message, **parameters},
            )
        )
        if self._budget is not None and _instance_side(caller.principal) is not None:
            self._budget.refused(caller.principal.id, time.monotonic())  # ATK-71
        return ToolResponse(
            False,
            failure=ToolFailure(_KIND.get(reason, "forbidden"), reason, message, remedy(reason)),
        )

    def _contract_import(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        imported = import_markdown(str(arguments["markdown"]))
        if not imported:
            raise Refusal(
                Reason.INVALID_REQUEST,
                "the document holds no requirement, constraint, story or criterion",
            )
        problems: list[Problem] = []
        for item in imported:
            problems.extend(validate_object(item))
        if problems:
            raise Refusal(Reason.INVALID_REQUEST, _problems_text(problems))
        operations = tuple(Operation("put", item, str(item["id"])) for item in imported)
        change = Change(str(arguments["summary"]), operations)
        version = self._contracts.apply_reserved(caller.principal, project_id, change)
        flagged = _flagged_terms(change)
        counts: dict[str, int] = {}
        for item in imported:
            kind = str(item["type"])
            counts[kind] = counts.get(kind, 0) + 1
        return ToolResult(
            {
                "version_id": version.id,
                "number": version.number,
                "objects": len(imported),
                "kinds": dict(sorted(counts.items())),
                "flagged": flagged,
            },
            {
                "project_id": project_id,
                "version_id": version.id,
                "objects": len(imported),
                "flagged": flagged,
            },
            project_id,
        )

    # ------------------------------------------------------------------ the staging area
    def staging_base(self, caller: Caller) -> tuple[bytes | None, ToolFailure | None]:
        """The base of the caller's project, as a package (GET /staging/base)."""

        def work(service: DeliveryService, project_id: str) -> tuple[bytes, dict[str, object]]:
            package = service.base_package(project_id)
            return package, {"bytes": len(package)}

        return self._transport(caller, Action.STAGING_READ, work)

    def push_delivery(
        self, caller: Caller, branch: str, package: bytes
    ) -> tuple[DeliveryRecord | None, ToolFailure | None]:
        """Keep a pushed branch (POST /staging/delivery). The Contractor only."""

        def work(
            service: DeliveryService, project_id: str
        ) -> tuple[DeliveryRecord, dict[str, object]]:
            record = service.push(project_id, package, branch)
            return record, {
                "delivery_id": record.delivery_id,
                "branch": record.branch,
                "bytes": len(package),
                "changed": len(record.changed_paths),
            }

        return self._transport(caller, Action.DELIVERY_PUSH, work)

    def _transport[V](
        self,
        caller: Caller,
        action: Action,
        work: Callable[[DeliveryService, str], tuple[V, dict[str, object]]],
    ) -> tuple[V | None, ToolFailure | None]:
        """One call that comes through a transport route rather than a tool: the same rights,
        the same single record."""
        project_id = caller.principal.project_id
        stream = self._stream(caller, project_id)
        decision = decide(caller.principal, action, project_id)
        if not decision.allowed or project_id is None:
            response = self._fail(caller, action, stream, decision.reason, "not allowed here", {})
            return None, response.failure
        if self._deliveries is None:
            response = self._fail(
                caller, action, stream, Reason.INVALID_REQUEST, "no delivery loop here", {}
            )
            return None, response.failure
        try:
            value, audit = work(self._deliveries, project_id)
        except Refusal as refusal:
            response = self._fail(caller, action, stream, refusal.reason, str(refusal), {})
            return None, response.failure
        self._audit.append(
            AuditEvent(
                stream,
                caller.principal.id,
                action,
                Outcome.OK,
                parameters={"source": caller.source, **audit},
            )
        )
        return value, None

    def _delivery_service(self) -> DeliveryService:
        if self._deliveries is None:
            raise Refusal(Reason.INVALID_REQUEST, "no delivery loop is configured on this hub")
        return self._deliveries

    def _project_prepare(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        source = self._delivery_service().prepare(
            project_id, str(arguments["repository"]), str(arguments["branch"])
        )
        data: dict[str, object] = {
            "project_id": project_id,
            "repository": source.repository,
            "branch": source.branch,
            "state": source.state,
        }
        return ToolResult(data, dict(data), project_id)

    def _acceptance_put(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        digest = self._delivery_service().put_acceptance(
            project_id, str(arguments["path"]), str(arguments["content"])
        )
        audit: dict[str, object] = {
            "project_id": project_id,
            "path": str(arguments["path"]),
            "sha256": digest,
        }
        return ToolResult(dict(audit), audit, project_id)

    def _delivery_declare(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        record = self._delivery_service().declare(project_id, str(arguments["delivery_id"]))
        if self._continuity is not None:
            self._continuity.answered(project_id, Side.CONTRACTOR.value)
        data: dict[str, object] = {
            "project_id": project_id,
            "delivery_id": record.delivery_id,
            "state": record.state.value,
        }
        return ToolResult(data, dict(data), project_id)

    def _delivery_status(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        record = self._delivery_service().status(project_id, str(arguments["delivery_id"]))
        data: dict[str, object] = {
            "project_id": project_id,
            "delivery_id": record.delivery_id,
            "branch": record.branch,
            "state": record.state.value,
            "decision": record.decision,
            "outcomes": dict(record.outcomes),
            "changed_paths": list(record.changed_paths),
            "pull_request": record.pull_request,
            "detail": record.detail,
            "report": record.report,
        }
        audit: dict[str, object] = {"project_id": project_id, "delivery_id": record.delivery_id}
        return ToolResult(data, audit, project_id)

    # ------------------------------------------------------------------ helpers for handlers
    @staticmethod
    def _project_of(caller: Caller, arguments: Mapping[str, object]) -> str:
        given = arguments.get("project_id")
        if isinstance(given, str):
            return given
        if caller.principal.project_id is not None:
            return caller.principal.project_id
        raise Refusal(Reason.INVALID_REQUEST, "project_id is required")

    def _known_project(self, caller: Caller, arguments: Mapping[str, object]) -> str:
        project_id = self._project_of(caller, arguments)
        self._projects.get(project_id)
        return project_id

    # ------------------------------------------------------------------ handlers
    def _whoami(self, caller: Caller, _arguments: Mapping[str, object]) -> ToolResult:
        principal = caller.principal
        data: dict[str, object] = {
            "principal": principal.id,
            "kind": principal.kind.value,
            "side": principal.side.value if principal.side else None,
            "project_id": principal.project_id,
        }
        return ToolResult(data, {})

    def _project_create(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        url = arguments.get("repository_url")
        if isinstance(url, str) and url and not source_accepted(url):
            raise Refusal(Reason.INVALID_REQUEST, SOURCE_REFUSAL)
        project = self._projects.create(caller.principal, arguments)
        return ToolResult(
            {"project_id": project.id, "name": project.name},
            {
                "project_id": project.id,
                "name": project.name,
                "has_repository_url": project.repository_url is not None,
            },
            project.id,
        )

    def _project_list(self, caller: Caller, _arguments: Mapping[str, object]) -> ToolResult:
        projects = self._projects.visible_to(caller.principal)
        data: dict[str, object] = {
            "projects": [{"project_id": p.id, "name": p.name} for p in projects]
        }
        return ToolResult(data, {"count": len(projects)})

    def _project_get(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project = self._projects.get(self._project_of(caller, arguments))
        data: dict[str, object] = {
            "project_id": project.id,
            "name": project.name,
            "repository_url": project.repository_url,
        }
        return ToolResult(data, {"project_id": project.id}, project.id)

    def _instance_start(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        side = Side(str(arguments["side"]))
        prompt = str(arguments["prompt"])
        info = self._instances.start(project_id, side, prompt)
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        return ToolResult(
            {"project_id": project_id, "side": side.value, "state": info.state.value},
            {
                "project_id": project_id,
                "side": side.value,
                "prompt_sha256": digest,
                "prompt_chars": len(prompt),
            },
            project_id,
        )

    def _instance_stop(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        side = Side(str(arguments["side"]))
        info = self._instances.stop(project_id, side)
        return ToolResult(
            {"project_id": project_id, "side": side.value, "state": info.state.value},
            {"project_id": project_id, "side": side.value},
            project_id,
        )

    def _instance_status(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        side = Side(str(arguments["side"]))
        info = self._instances.status(project_id, side)
        return ToolResult(
            {
                "project_id": project_id,
                "side": side.value,
                "state": info.state.value,
                "turn_started": info.turn_started,
                "last_turn": info.last_turn,
                "address": info.address,
            },
            {"project_id": project_id, "side": side.value},
            project_id,
        )

    def _contract_read(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        number = arguments.get("version")
        version = self._contracts.read(
            caller.principal, project_id, None if number is None else int(str(number))
        )
        data: dict[str, object] = {
            "project_id": project_id,
            "number": version.number,
            "version_id": version.id,
            "parent": version.parent,
            "content_hash": version.content_hash,
            "author": version.author,
            "objects": {key: dict(item) for key, item in version.objects.items()},
        }
        return ToolResult(data, {"project_id": project_id, "version_id": version.id}, project_id)

    def _contract_propose(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        change = parse_change(
            {"summary": arguments["summary"], "operations": arguments["operations"]}
        )
        pending = self._contracts.propose(caller.principal, project_id, change)
        if self._continuity is not None:
            self._continuity.expect(
                TaskState(
                    project_id,
                    Side.OWNER.value,
                    "decision",
                    f"change {pending.id} of version {pending.base_version} awaits a decision",
                    self._clock.now(),
                )
            )
        flagged = _flagged_terms(change)
        return ToolResult(
            {
                "change_id": pending.id,
                "base_version": pending.base_version,
                "status": pending.status.value,
                "flagged": flagged,
            },
            {
                "project_id": project_id,
                "change_id": pending.id,
                "base_version": pending.base_version,
                "operations": len(change.operations),
                "summary": change.summary,
                "flagged": flagged,
            },
            project_id,
        )

    def _contract_approve(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        version = self._contracts.approve(caller.principal, project_id, str(arguments["change_id"]))
        if self._continuity is not None:
            self._continuity.answered(project_id, Side.OWNER.value)
        return ToolResult(
            {"version_id": version.id, "number": version.number},
            {
                "project_id": project_id,
                "change_id": str(arguments["change_id"]),
                "approver": caller.principal.id,
                "version_id": version.id,
            },
            project_id,
        )

    def _contract_reject(self, caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        resolved = self._contracts.reject(caller.principal, project_id, str(arguments["change_id"]))
        if self._continuity is not None:
            self._continuity.answered(project_id, Side.OWNER.value)
        return ToolResult(
            {"change_id": resolved.id, "status": resolved.status.value},
            {
                "project_id": project_id,
                "change_id": resolved.id,
                "rejected_by": caller.principal.id,
            },
            project_id,
        )

    def _contract_apply_reserved(
        self, caller: Caller, arguments: Mapping[str, object]
    ) -> ToolResult:
        project_id = self._known_project(caller, arguments)
        change = parse_change(
            {"summary": arguments["summary"], "operations": arguments["operations"]}
        )
        version = self._contracts.apply_reserved(caller.principal, project_id, change)
        flagged = _flagged_terms(change)
        return ToolResult(
            {"version_id": version.id, "number": version.number, "flagged": flagged},
            {
                "project_id": project_id,
                "version_id": version.id,
                "operations": len(change.operations),
                "summary": change.summary,
                "flagged": flagged,
            },
            project_id,
        )

    def _log_read(self, _caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        stream = str(arguments["stream"])
        after = int(str(arguments.get("after", 0)))
        limit = int(str(arguments.get("limit", 100)))
        records = self._audit.read(stream, after, limit)
        return ToolResult(
            {"stream": stream, "records": [format_line(record) for record in records]},
            {"stream": stream, "after": after, "count": len(records)},
        )

    def _log_verify(self, _caller: Caller, arguments: Mapping[str, object]) -> ToolResult:
        report = self._audit.verify(str(arguments["stream"]))
        data: dict[str, object] = {
            "stream": report.stream,
            "ok": report.ok,
            "records": report.records,
            "first_bad_seq": report.first_bad_seq,
            "problem": report.problem,
        }
        return ToolResult(data, {"stream": report.stream, "ok": report.ok})
