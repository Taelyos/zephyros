"""The rights matrix: deny by default, scope by project (EXG-01, EXG-02; US-E1-07)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zephyros.core.audit import Action, Reason
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import (
    MAIN_AGENT,
    OPERATOR,
    Principal,
    PrincipalKind,
    instance_principal,
)
from zephyros.core.rights import PERMISSIONS, Scope, allowed_actions, decide

PROJECT = "p_abcdef12"
OTHER = "p_zzzzzz99"
OWNER = instance_principal(Side.OWNER, PROJECT)
CONTRACTOR = instance_principal(Side.CONTRACTOR, PROJECT)

# The allow-list of rights (docs/tools.md), written out here independently of the code.
EXPECTED = {
    "whoami": ("any", "any", "any", "any"),
    "project.create": ("any", "any", "none", "none"),
    "project.list": ("any", "any", "own", "own"),
    "project.get": ("any", "any", "own", "own"),
    "instance.start": ("any", "any", "none", "none"),
    "instance.stop": ("any", "any", "none", "none"),
    "instance.status": ("any", "any", "none", "none"),
    "contract.read": ("any", "any", "own", "own"),
    "contract.propose": ("any", "any", "own", "none"),
    "contract.approve": ("any", "any", "own", "none"),
    "contract.reject": ("any", "any", "own", "none"),
    "contract.pending": ("any", "any", "own", "none"),
    "contract.apply_reserved": ("any", "none", "none", "none"),
    "contract.import": ("any", "none", "none", "none"),
    "gateway.call": ("none", "none", "own", "own"),
    "log.read": ("any", "none", "none", "none"),
    "log.verify": ("any", "none", "none", "none"),
    "project.prepare": ("any", "any", "none", "none"),
    "acceptance.put": ("any", "none", "own", "none"),
    "staging.read": ("none", "none", "own", "own"),
    "delivery.push": ("none", "none", "none", "own"),
    "delivery.declare": ("none", "none", "none", "own"),
    "delivery.status": ("any", "any", "own", "own"),
    "inspection.record": ("any", "any", "own", "none"),
    "delivery.review": ("any", "any", "own", "none"),
}
KINDS = (
    PrincipalKind.OPERATOR,
    PrincipalKind.MAIN_AGENT,
    PrincipalKind.OWNER,
    PrincipalKind.CONTRACTOR,
)


def test_the_matrix_is_the_documented_allow_list() -> None:
    actual = {
        action.value: tuple(row[kind].value for kind in KINDS)
        for action, row in PERMISSIONS.items()
    }
    assert actual == EXPECTED


@pytest.mark.parametrize(("name", "scopes"), EXPECTED.items())
def test_every_cell_decides_as_written(name: str, scopes: tuple[str, ...]) -> None:
    principals = (OPERATOR, MAIN_AGENT, OWNER, CONTRACTOR)
    for principal, scope in zip(principals, scopes, strict=True):
        own = decide(principal, Action(name), PROJECT)
        foreign = decide(principal, Action(name), OTHER)
        assert own.allowed is (scope != "none")
        if scope == "any":
            assert foreign.allowed
        else:
            assert not foreign.allowed
            assert foreign.reason in (Reason.FORBIDDEN, Reason.OTHER_PROJECT)
        if scope == "own" and principal.project_id == PROJECT:
            assert foreign.reason is Reason.OTHER_PROJECT


def test_an_instance_reaches_its_own_project_and_no_other() -> None:
    assert decide(CONTRACTOR, Action.CONTRACT_READ, PROJECT).allowed
    refused = decide(CONTRACTOR, Action.CONTRACT_READ, OTHER)
    assert (refused.allowed, refused.reason) == (False, Reason.OTHER_PROJECT)
    assert decide(OWNER, Action.PROJECT_LIST, None).allowed


def test_the_contractor_has_no_tool_that_writes_the_contract_or_the_rules() -> None:
    reachable = {action.value for action in allowed_actions(PrincipalKind.CONTRACTOR)}
    assert reachable == {
        "whoami",
        "project.list",
        "project.get",
        "contract.read",
        "gateway.call",
        "staging.read",
        "delivery.push",
        "delivery.declare",
        "delivery.status",
    }
    writes = {"contract.propose", "contract.approve", "contract.reject", "contract.apply_reserved"}
    assert not writes & reachable
    assert "acceptance.put" not in reachable  # the Contractor never writes what verifies it


def test_only_the_operator_applies_reserved_data_and_reads_the_log() -> None:
    for kind in KINDS[1:]:
        assert Action.CONTRACT_APPLY_RESERVED not in allowed_actions(kind)
        assert Action.LOG_READ not in allowed_actions(kind)
        assert Action.LOG_VERIFY not in allowed_actions(kind)
    assert Action.CONTRACT_APPLY_RESERVED in allowed_actions(PrincipalKind.OPERATOR)


def test_an_action_that_is_not_in_the_matrix_is_denied() -> None:
    for action in (Action.UNKNOWN, Action.TOKEN_ISSUE, Action.AUTHENTICATION, Action.TURN_START):
        for principal in (OPERATOR, MAIN_AGENT, OWNER, CONTRACTOR):
            decision = decide(principal, action, PROJECT)
            assert (decision.allowed, decision.reason, decision.scope) == (
                False,
                Reason.FORBIDDEN,
                Scope.NONE,
            )


def test_a_scope_of_own_without_a_project_is_denied() -> None:
    # Cannot happen with a well-formed principal; the function still refuses.
    odd = Principal("operator", PrincipalKind.OPERATOR)
    object.__setattr__(odd, "kind", PrincipalKind.OWNER)
    assert not decide(odd, Action.CONTRACT_READ, PROJECT).allowed


@given(
    action=st.sampled_from(list(Action)),
    side=st.sampled_from([Side.OWNER, Side.CONTRACTOR]),
    name=st.sampled_from(["main", "b2", "x9"]),
    mine=st.from_regex(r"p_[a-z0-9]{6,12}", fullmatch=True),
    theirs=st.from_regex(r"p_[a-z0-9]{6,12}", fullmatch=True),
)
def test_an_instance_is_never_allowed_on_another_project(
    action: Action, side: Side, name: str, mine: str, theirs: str
) -> None:
    principal = instance_principal(side, mine, name)
    decision = decide(principal, action, theirs)
    if mine != theirs:
        assert not decision.allowed or PERMISSIONS[action][principal.kind] is Scope.ANY
        if decision.allowed:
            # An instance has no row with scope 'any' in the matrix, except whoami.
            assert action is Action.WHOAMI
