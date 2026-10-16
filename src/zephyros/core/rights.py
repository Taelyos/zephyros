"""Who may do what: one pure function, deny by default (EXG-01, EXG-02; US-E1-07).

`PERMISSIONS` is the allow-list of the tools of the hub by kind of principal. It is the
documented list of what an instance reaches (docs/tools.md is generated from it), and a test
fails when the registry of tools and this table differ. A principal with scope `own` acts on
the project of its token and on no other. Identity comes from the token record, never from the
request, so a project identifier in a request is a resource to check, not a claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from zephyros.core.audit import Action, Reason
from zephyros.core.principals import Principal, PrincipalKind


class Scope(StrEnum):
    NONE = "none"
    OWN = "own"
    ANY = "any"


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: Reason
    scope: Scope


_O, _M, _W, _C = (
    PrincipalKind.OPERATOR,
    PrincipalKind.MAIN_AGENT,
    PrincipalKind.OWNER,
    PrincipalKind.CONTRACTOR,
)
_ALL = Scope.ANY
_OWN = Scope.OWN
_NO = Scope.NONE


def _row(
    operator: Scope, main: Scope, owner: Scope, contractor: Scope
) -> Mapping[PrincipalKind, Scope]:
    return MappingProxyType({_O: operator, _M: main, _W: owner, _C: contractor})


PERMISSIONS: Mapping[Action, Mapping[PrincipalKind, Scope]] = MappingProxyType(
    {
        Action.WHOAMI: _row(_ALL, _ALL, _ALL, _ALL),
        Action.PROJECT_CREATE: _row(_ALL, _ALL, _NO, _NO),
        Action.PROJECT_LIST: _row(_ALL, _ALL, _OWN, _OWN),
        Action.PROJECT_GET: _row(_ALL, _ALL, _OWN, _OWN),
        Action.INSTANCE_START: _row(_ALL, _ALL, _NO, _NO),
        Action.INSTANCE_STOP: _row(_ALL, _ALL, _NO, _NO),
        Action.INSTANCE_STATUS: _row(_ALL, _ALL, _NO, _NO),
        Action.CONTRACT_READ: _row(_ALL, _ALL, _OWN, _OWN),
        Action.CONTRACT_PROPOSE: _row(_ALL, _ALL, _OWN, _NO),
        Action.CONTRACT_APPROVE: _row(_ALL, _ALL, _OWN, _NO),
        Action.CONTRACT_REJECT: _row(_ALL, _ALL, _OWN, _NO),
        Action.CONTRACT_PENDING: _row(_ALL, _ALL, _OWN, _NO),
        Action.CONTRACT_APPLY_RESERVED: _row(_ALL, _NO, _NO, _NO),
        Action.CONTRACT_IMPORT: _row(_ALL, _NO, _NO, _NO),
        Action.GATEWAY_CALL: _row(_NO, _NO, _OWN, _OWN),
        Action.LOG_READ: _row(_ALL, _NO, _NO, _NO),
        Action.LOG_VERIFY: _row(_ALL, _NO, _NO, _NO),
        Action.PROJECT_PREPARE: _row(_ALL, _ALL, _NO, _NO),
        Action.ACCEPTANCE_PUT: _row(_ALL, _NO, _OWN, _NO),
        Action.STAGING_READ: _row(_NO, _NO, _OWN, _OWN),
        Action.DELIVERY_PUSH: _row(_NO, _NO, _NO, _OWN),
        Action.DELIVERY_DECLARE: _row(_NO, _NO, _NO, _OWN),
        Action.DELIVERY_STATUS: _row(_ALL, _ALL, _OWN, _OWN),
        Action.INSPECTION_RECORD: _row(_ALL, _ALL, _OWN, _NO),
        Action.DELIVERY_REVIEW: _row(_ALL, _ALL, _OWN, _NO),
        # ADR 0034: the rows say who may use each tool; which obstacles a principal reads,
        # answers or closes follows who they concern (`core/obstacles.py`).
        Action.OBSTACLE_REPORT: _row(_NO, _NO, _OWN, _OWN),
        Action.OBSTACLE_LIST: _row(_ALL, _ALL, _OWN, _OWN),
        Action.OBSTACLE_READ: _row(_ALL, _ALL, _OWN, _OWN),
        Action.OBSTACLE_ANSWER: _row(_ALL, _ALL, _OWN, _NO),
        Action.OBSTACLE_CLOSE: _row(_ALL, _ALL, _OWN, _OWN),
    }
)


def decide(principal: Principal, action: Action, project_id: str | None = None) -> Decision:
    """May `principal` perform `action` on the resource of `project_id` (None for a resource
    that belongs to no project, such as the list of projects)? Deny by default."""
    row = PERMISSIONS.get(action)
    if row is None:
        return Decision(False, Reason.FORBIDDEN, Scope.NONE)
    scope = row[principal.kind]
    if scope is Scope.NONE:
        return Decision(False, Reason.FORBIDDEN, Scope.NONE)
    if scope is Scope.ANY:
        return Decision(True, Reason.NONE, Scope.ANY)
    if principal.project_id is None:
        return Decision(False, Reason.FORBIDDEN, Scope.NONE)
    if project_id is not None and project_id != principal.project_id:
        return Decision(False, Reason.OTHER_PROJECT, Scope.OWN)
    return Decision(True, Reason.NONE, Scope.OWN)


def allowed_actions(kind: PrincipalKind) -> list[Action]:
    """The actions a kind of principal may take, in a stable order: the documented allow-list."""
    return [action for action, row in PERMISSIONS.items() if row[kind] is not Scope.NONE]
