"""The contract registry (EXG-07, EXG-15; US-E2-01, US-E2-02; ATK-88, ATK-89).

The contract of a project is a set of typed objects (requirements, constraints, stories,
criteria, links, notes), versioned as immutable snapshots. A version is changed in one of two
ways:

* the operator applies a change that touches operator-reserved data, directly;
* any other change is proposed, then approved or rejected by a principal other than its
  proposer. While it is pending, the current version stays in force.

Operator-reserved data is the text, the priority and the verification methods of a
requirement or a constraint, the text of each P0 criterion, and each link that touches a P0
requirement. It is computed as a *view* of the objects. A change touches reserved data when
the view of the result differs from the view before, so no spelling of a change can slip
past: removing a link to a P0 requirement, adding a criterion to a story that covers one, or
changing a priority all change the view.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from zephyros.core.audit import Action, Reason, canonical_json
from zephyros.core.clock import Clock
from zephyros.core.errors import Refusal
from zephyros.core.principals import Principal, PrincipalKind
from zephyros.core.rights import decide
from zephyros.core.schema import (
    JSON_SCHEMA_DIALECT,
    Array,
    Choice,
    Field,
    Free,
    Problem,
    Table,
    Text,
)

CONTRACT_SCHEMA_VERSION = "1.1.0"
TEST_ID = r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}::[A-Za-z0-9_][A-Za-z0-9_.\[\]-]{0,80}"
"""An acceptance test as a kit names it: a relative path, `::`, a name."""
CONTRACT_SCHEMA_ID = "https://github.com/Taelyos/zephyros/schemas/contract.schema.json"

METHODS = ("inspection", "analysis", "demonstration", "test")
PRIORITIES = ("P0", "P1", "P2", "P3")
MAX_OPERATIONS = 200
MAX_TEXT = 4000
MAX_SUMMARY = 300

_ID_REQUIREMENT = r"EXG-[0-9]{2,3}"
_ID_CONSTRAINT = r"CTR-[0-9]{2,3}"
_ID_STORY = r"US-E[0-9]{1,2}-[0-9]{2}"
_ID_CRITERION = r"US-E[0-9]{1,2}-[0-9]{2}\.[0-9]{1,2}"
_ID_NOTE = r"NB-[0-9]{3}"
_TARGET = f"(?:{_ID_REQUIREMENT}|{_ID_CONSTRAINT}|{_ID_STORY}|{_ID_CRITERION})"
_ID_LINK = f"LNK:{_ID_STORY}>(?:{_ID_REQUIREMENT}|{_ID_CONSTRAINT})"


def _kind(name: str) -> Field:
    return Field("type", Choice((name,)), "The kind of the object", required=True)


def _text(description: str = "Text") -> Field:
    return Field(
        "text", Text(min_length=1, max_length=MAX_TEXT, multiline=True), description, required=True
    )


_METHODS_FIELD = Field(
    "methods",
    Array(Choice(METHODS), min_items=1, max_items=2, unique=True),
    "One to two verification methods",
    required=True,
)
_PRIORITY_FIELD = Field("priority", Choice(PRIORITIES), "Priority", required=True)

OBJECT_SPECS: Mapping[str, Table] = {
    "requirement": Table(
        (
            _kind("requirement"),
            Field("id", Text(pattern=_ID_REQUIREMENT), "EXG-nn", required=True),
            _text("The requirement, in the form 'The system shall ...'"),
            _PRIORITY_FIELD,
            _METHODS_FIELD,
        )
    ),
    "constraint": Table(
        (
            _kind("constraint"),
            Field("id", Text(pattern=_ID_CONSTRAINT), "CTR-nn", required=True),
            _text("The constraint"),
            _PRIORITY_FIELD,
            _METHODS_FIELD,
        )
    ),
    "story": Table(
        (
            _kind("story"),
            Field("id", Text(pattern=_ID_STORY), "US-Enn-nn", required=True),
            Field("epic", Text(pattern=r"E[0-9]{1,2}"), "The epic", required=True),
            Field("title", Text(min_length=1, max_length=200), "A short title", required=True),
            _text("As a ..., I want ..., so that ..."),
        )
    ),
    "criterion": Table(
        (
            _kind("criterion"),
            Field("id", Text(pattern=_ID_CRITERION), "US-Enn-nn.k", required=True),
            Field("story", Text(pattern=_ID_STORY), "The story it belongs to", required=True),
            _text("The acceptance criterion"),
            Field(
                "verification",
                Text(max_length=80, pattern=r"[A-Za-z0-9 ,()-]*"),
                "How it is verified: an attack identifier or a method letter",
            ),
            Field(
                "test",
                Text(max_length=200, pattern=TEST_ID),
                "The acceptance test that verifies it, as its kit names it (path::name)",
            ),
        )
    ),
    "link": Table(
        (
            _kind("link"),
            Field("id", Text(pattern=_ID_LINK), "LNK:<story>><requirement>", required=True),
            Field("kind", Choice(("covers",)), "What the link states", required=True),
            Field("source", Text(pattern=_ID_STORY), "The story", required=True),
            Field(
                "target",
                Text(pattern=f"{_ID_REQUIREMENT}|{_ID_CONSTRAINT}"),
                "The requirement or constraint",
                required=True,
            ),
        )
    ),
    "note": Table(
        (
            _kind("note"),
            Field("id", Text(pattern=_ID_NOTE), "NB-nnn", required=True),
            Field("target", Text(pattern=_TARGET), "The object it is about", required=True),
            _text("The note"),
        )
    ),
}
SCHEMA_TABLE = Table(
    tuple(Field(name, spec, f"A {name} object") for name, spec in OBJECT_SPECS.items())
)

Objects = Mapping[str, Mapping[str, object]]


def contract_json_schema() -> dict[str, object]:
    """The JSON Schema of one contract object: one of six kinds, each with a closed set of
    fields. Published in schemas/ with a version number."""
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": CONTRACT_SCHEMA_ID,
        "title": "Zephyros contract object",
        "x-zephyros-schema-version": CONTRACT_SCHEMA_VERSION,
        "oneOf": [{"title": name, **spec.json_schema()} for name, spec in OBJECT_SPECS.items()],
    }


class ContractError(Exception):
    """A change or an object is invalid. `problems` names each fault."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(str(problem) for problem in problems))


# ----------------------------------------------------------------------------- objects
def validate_object(value: object, key: str = "object") -> list[Problem]:
    """Problems of one contract object: a closed set of fields, by kind."""
    if not isinstance(value, Mapping):
        return [Problem(key, "must be an object")]
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in OBJECT_SPECS:
        return [Problem(f"{key}.type", "must be one of: " + ", ".join(OBJECT_SPECS))]
    return OBJECT_SPECS[kind].check(value, key)


def check_integrity(objects: Objects) -> list[Problem]:
    """References between objects: every end of a link, every story of a criterion and every
    target of a note exists, and an identifier is the key it is stored under."""
    problems: list[Problem] = []
    for key, item in objects.items():
        if item.get("id") != key:
            problems.append(Problem(key, "the identifier differs from the key"))
        kind = item.get("type")
        if kind == "link":
            source, target = str(item["source"]), str(item["target"])
            if item["id"] != f"LNK:{source}>{target}":
                problems.append(Problem(key, "a link is identified by its two ends"))
            if objects.get(source, {}).get("type") != "story":
                problems.append(Problem(key, f"{source} is not a story of the contract"))
            if objects.get(target, {}).get("type") not in ("requirement", "constraint"):
                problems.append(Problem(key, f"{target} is not a requirement or constraint"))
        elif kind == "criterion":
            story = str(item["story"])
            if objects.get(story, {}).get("type") != "story":
                problems.append(Problem(key, f"{story} is not a story of the contract"))
            elif not str(item["id"]).startswith(story + "."):
                problems.append(Problem(key, "a criterion is numbered under its story"))
        elif kind == "note" and str(item["target"]) not in objects:
            problems.append(Problem(key, f"{item['target']} is not an object of the contract"))
    return problems


def p0_ids(objects: Objects) -> set[str]:
    return {
        key
        for key, item in objects.items()
        if item.get("type") in ("requirement", "constraint") and item.get("priority") == "P0"
    }


def p0_criteria(objects: Objects) -> set[str]:
    """The P0 criteria: those of a story that covers at least one P0 requirement."""
    p0 = p0_ids(objects)
    stories = {
        str(item["source"])
        for item in objects.values()
        if item.get("type") == "link" and item.get("target") in p0
    }
    return {
        key
        for key, item in objects.items()
        if item.get("type") == "criterion" and item.get("story") in stories
    }


def reserved_view(objects: Objects) -> dict[str, object]:
    """The operator-reserved data of a set of objects, as comparable data.

    Everything that only the operator may change is in this view and nothing else is: the
    text, priority and methods of each requirement and constraint (and so their existence),
    each link that touches a P0 requirement, and each P0 criterion in full.
    """
    p0 = p0_ids(objects)
    criteria = p0_criteria(objects)
    view: dict[str, object] = {}
    for key, item in objects.items():
        kind = item.get("type")
        if (
            kind in ("requirement", "constraint")
            or (kind == "link" and item.get("target") in p0)
            or key in criteria
        ):
            view[key] = dict(item)
    return view


# ----------------------------------------------------------------------------- versions
@dataclass(frozen=True, slots=True)
class ContractVersion:
    number: int
    id: str
    parent: str | None
    content_hash: str
    objects: Mapping[str, Mapping[str, object]]
    author: str
    time: datetime
    operator_written: bool


def content_hash(objects: Objects) -> str:
    return hashlib.sha256(
        canonical_json({k: dict(v) for k, v in sorted(objects.items())}).encode("ascii")
    ).hexdigest()


def version_id(number: int, digest: str) -> str:
    """A counter and the start of the content hash: it differs from every earlier version."""
    return f"v{number}-{digest[:12]}"


EMPTY_OBJECTS: Objects = {}


def empty_version(time: datetime) -> ContractVersion:
    digest = content_hash(EMPTY_OBJECTS)
    return ContractVersion(0, version_id(0, digest), None, digest, {}, "hub", time, True)


# ----------------------------------------------------------------------------- changes
@dataclass(frozen=True, slots=True)
class Operation:
    op: str
    """'put' (create or replace an object) or 'delete'."""
    object: Mapping[str, object] | None = None
    id: str | None = None


@dataclass(frozen=True, slots=True)
class Change:
    summary: str
    operations: tuple[Operation, ...]


class ChangeStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PendingChange:
    id: str
    project_id: str
    proposer: str
    base_version: int
    summary: str
    operations: tuple[Operation, ...]
    status: ChangeStatus
    created_at: datetime
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resulting_version: str | None = None


_OPERATION_PUT = Table(
    (
        Field("op", Choice(("put",)), "Create or replace an object", required=True),
        Field("object", Free(), "The object", required=True),
    )
)
_OPERATION_DELETE = Table(
    (
        Field("op", Choice(("delete",)), "Delete an object", required=True),
        Field("id", Text(max_length=64), "The identifier of the object", required=True),
    )
)
_CHANGE_TOP = Table(
    (
        Field(
            "summary",
            Text(min_length=1, max_length=MAX_SUMMARY, multiline=True),
            "Why the change is made",
            required=True,
        ),
        Field(
            "operations",
            Array(Free(), min_items=1, max_items=MAX_OPERATIONS),
            "What the change does",
            required=True,
        ),
    )
)


def parse_change(data: object) -> Change:
    """Validate the body of a change: closed fields, bounded size, valid objects.

    Raises ContractError naming each fault; an unknown field is a fault.
    """
    problems = _CHANGE_TOP.check(data, "change")
    if problems:
        raise ContractError(problems)
    assert isinstance(data, Mapping)  # noqa: S101  # reason: narrowed by the check above
    operations: list[Operation] = []
    faults: list[Problem] = []
    for index, raw in enumerate(data["operations"]):
        key = f"change.operations[{index}]"
        is_put = raw.get("op") == "put"
        item_problems = (_OPERATION_PUT if is_put else _OPERATION_DELETE).check(raw, key)
        if item_problems:
            faults.extend(item_problems)
        elif is_put:
            object_problems = validate_object(raw["object"], f"{key}.object")
            faults.extend(object_problems)
            if not object_problems:
                operations.append(Operation("put", object=dict(raw["object"])))
        else:
            operations.append(Operation("delete", id=str(raw["id"])))
    if faults:
        raise ContractError(faults)
    return Change(str(data["summary"]), tuple(operations))


def apply_operations(
    objects: Objects, operations: Sequence[Operation]
) -> dict[str, dict[str, object]]:
    """The objects after the operations, or ContractError. The input is not modified."""
    result: dict[str, dict[str, object]] = {key: dict(item) for key, item in objects.items()}
    faults: list[Problem] = []
    for index, operation in enumerate(operations):
        key = f"change.operations[{index}]"
        if operation.op == "put" and operation.object is not None:
            result[str(operation.object["id"])] = dict(operation.object)
        elif operation.op == "delete" and operation.id is not None:
            if operation.id in result:
                del result[operation.id]
            else:
                faults.append(Problem(key, f"{operation.id} is not in the contract"))
        else:
            faults.append(Problem(key, "a malformed operation"))
    if faults:
        raise ContractError(faults)
    integrity = check_integrity(result)
    if integrity:
        raise ContractError(integrity)
    return result


def operations_to_data(operations: Sequence[Operation]) -> list[dict[str, object]]:
    return [
        {"op": "put", "object": dict(o.object)}
        if o.op == "put" and o.object is not None
        else {"op": "delete", "id": o.id}
        for o in operations
    ]


def operations_from_data(data: Sequence[Mapping[str, object]]) -> tuple[Operation, ...]:
    return tuple(
        Operation("put", object=dict(item["object"]))  # type: ignore[call-overload]  # reason: stored data was validated when written
        if item["op"] == "put"
        else Operation("delete", id=str(item["id"]))
        for item in data
    )


# ----------------------------------------------------------------------------- store
class ContractStore(Protocol):
    def latest(self, project_id: str) -> ContractVersion | None: ...

    def get(self, project_id: str, number: int) -> ContractVersion | None: ...

    def numbers(self, project_id: str) -> list[int]: ...

    def append(self, project_id: str, version: ContractVersion) -> None:
        """Add the version that follows the latest. Fails when the number is not the next."""
        ...

    def add_pending(self, change: PendingChange) -> None: ...

    def get_pending(self, project_id: str, change_id: str) -> PendingChange | None: ...

    def pending(self, project_id: str) -> list[PendingChange]: ...

    def reject(self, project_id: str, change_id: str, by: str, at: datetime) -> bool:
        """Mark a pending change rejected. False when it is not pending."""
        ...

    def approve(
        self, project_id: str, change_id: str, by: str, at: datetime, version: ContractVersion
    ) -> bool:
        """In one step, add `version` and mark the change approved. False when not pending."""
        ...


# ----------------------------------------------------------------------------- service
class ContractService:
    """The operations on the contract, with the rights and the floor of the operator."""

    def __init__(
        self,
        store: ContractStore,
        clock: Clock,
        *,
        new_change_id: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_change_id = new_change_id or (lambda: "c_" + secrets.token_hex(4))

    # ------------------------------------------------------------------ guards
    def _allow(self, principal: Principal, action: Action, project_id: str) -> None:
        decision = decide(principal, action, project_id)
        if not decision.allowed:
            raise Refusal(decision.reason, f"{principal.kind.value} may not {action.value} here")

    def _current(self, project_id: str) -> ContractVersion:
        return self._store.latest(project_id) or empty_version(self._clock.now())

    # ------------------------------------------------------------------ reads
    def read(
        self, principal: Principal, project_id: str, number: int | None = None
    ) -> ContractVersion:
        self._allow(principal, Action.CONTRACT_READ, project_id)
        if number is None:
            return self._current(project_id)
        version = (
            empty_version(self._clock.now()) if number == 0 else self._store.get(project_id, number)
        )
        if version is None:
            raise Refusal(Reason.NOT_FOUND, f"there is no version {number}")
        return version

    def pending(self, principal: Principal, project_id: str) -> list[PendingChange]:
        """The changes that await a decision: whoever may decide reads them first (ADR 0020)."""
        self._allow(principal, Action.CONTRACT_PENDING, project_id)
        return self._store.pending(project_id)

    # ------------------------------------------------------------------ writes
    def _new_version(
        self, current: ContractVersion, objects: Objects, author: Principal
    ) -> ContractVersion:
        digest = content_hash(objects)
        number = current.number + 1
        return ContractVersion(
            number=number,
            id=version_id(number, digest),
            parent=current.id if current.number else None,
            content_hash=digest,
            objects={key: dict(item) for key, item in objects.items()},
            author=author.id,
            time=self._clock.now(),
            operator_written=author.kind is PrincipalKind.OPERATOR,
        )

    def _apply(self, current: ContractVersion, change: Change) -> dict[str, dict[str, object]]:
        try:
            return apply_operations(current.objects, change.operations)
        except ContractError as error:
            raise Refusal(Reason.INVALID_REQUEST, str(error)) from error

    def apply_reserved(
        self, principal: Principal, project_id: str, change: Change
    ) -> ContractVersion:
        """The operator applies a change that touches reserved data, as a new version."""
        self._allow(principal, Action.CONTRACT_APPLY_RESERVED, project_id)
        current = self._current(project_id)
        result = self._apply(current, change)
        if reserved_view(result) == reserved_view(current.objects):
            raise Refusal(
                Reason.INVALID_REQUEST,
                "this change touches no operator-reserved data: propose it instead",
            )
        version = self._new_version(current, result, principal)
        self._store.append(project_id, version)
        return version

    def propose(self, principal: Principal, project_id: str, change: Change) -> PendingChange:
        """Record a change as pending. The current version stays in force."""
        self._allow(principal, Action.CONTRACT_PROPOSE, project_id)
        current = self._current(project_id)
        result = self._apply(current, change)
        if reserved_view(result) != reserved_view(current.objects):
            raise Refusal(
                Reason.RESERVED_DATA,
                "the change touches operator-reserved data, which only the operator changes",
            )
        pending = PendingChange(
            id=self._new_change_id(),
            project_id=project_id,
            proposer=principal.id,
            base_version=current.number,
            summary=change.summary,
            operations=change.operations,
            status=ChangeStatus.PENDING,
            created_at=self._clock.now(),
        )
        self._store.add_pending(pending)
        return pending

    def _pending(self, project_id: str, change_id: str) -> PendingChange:
        pending = self._store.get_pending(project_id, change_id)
        if pending is None:
            raise Refusal(Reason.NOT_FOUND, "there is no such change in this project")
        if pending.status is not ChangeStatus.PENDING:
            raise Refusal(Reason.NOT_PENDING, f"the change is already {pending.status.value}")
        return pending

    def approve(self, principal: Principal, project_id: str, change_id: str) -> ContractVersion:
        """Apply a pending change as a new version. The proposer cannot approve its own change."""
        self._allow(principal, Action.CONTRACT_APPROVE, project_id)
        pending = self._pending(project_id, change_id)
        if principal.id == pending.proposer:
            raise Refusal(Reason.SELF_APPROVAL, "the proposer of a change cannot approve it")
        current = self._current(project_id)
        if pending.base_version != current.number:
            raise Refusal(Reason.STALE_BASE, "the contract changed since the change was proposed")
        result = self._apply(current, Change(pending.summary, pending.operations))
        if reserved_view(result) != reserved_view(current.objects):
            raise Refusal(Reason.RESERVED_DATA, "the change touches operator-reserved data")
        version = self._new_version(current, result, principal)
        if not self._store.approve(project_id, change_id, principal.id, self._clock.now(), version):
            raise Refusal(Reason.NOT_PENDING, "the change is no longer pending")
        return version

    def reject(self, principal: Principal, project_id: str, change_id: str) -> PendingChange:
        """Close a pending change without applying it. The current version stays."""
        self._allow(principal, Action.CONTRACT_REJECT, project_id)
        self._pending(project_id, change_id)
        if not self._store.reject(project_id, change_id, principal.id, self._clock.now()):
            raise Refusal(Reason.NOT_PENDING, "the change is no longer pending")
        resolved = self._store.get_pending(project_id, change_id)
        assert resolved is not None  # noqa: S101  # reason: it was pending a moment ago
        return resolved


def check_floor(versions: Sequence[ContractVersion]) -> list[str]:
    """Faults of a history of versions: a version written by anyone but the operator that
    changed reserved data, an identifier that repeats, a parent that is not the previous
    version. Used by tests and by the verification tool."""
    faults: list[str] = []
    seen: set[str] = set()
    previous: ContractVersion | None = None
    for version in versions:
        if version.id in seen:
            faults.append(f"{version.id}: the identifier repeats")
        seen.add(version.id)
        before = previous.objects if previous else EMPTY_OBJECTS
        if previous is not None and version.parent != previous.id:
            faults.append(f"{version.id}: the parent is not the previous version")
        if not version.operator_written and reserved_view(version.objects) != reserved_view(before):
            faults.append(f"{version.id}: reserved data changed without the operator")
        previous = version
    return faults
