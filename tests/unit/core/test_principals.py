"""Principals: stable identifiers for the operator, the main agent and the instances."""

from __future__ import annotations

import pytest

from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import (
    MAIN_AGENT,
    OPERATOR,
    Principal,
    PrincipalKind,
    instance_principal,
    is_project_id,
    parse_principal,
)

PROJECT = "p_abcdef12"


def test_instance_principals_are_stable_and_named() -> None:
    owner = instance_principal(Side.OWNER, PROJECT)
    contractor = instance_principal(Side.CONTRACTOR, PROJECT)
    assert owner.id == f"owner:{PROJECT}"
    assert contractor.id == f"contractor:{PROJECT}"
    assert owner.side is Side.OWNER
    assert contractor.side is Side.CONTRACTOR
    assert owner.is_instance
    assert instance_principal(Side.OWNER, PROJECT) == owner


def test_a_second_instance_of_a_side_is_another_principal() -> None:
    second = instance_principal(Side.OWNER, PROJECT, "two")
    assert second.id == f"owner:{PROJECT}:two"
    assert second != instance_principal(Side.OWNER, PROJECT)


def test_the_operator_and_the_main_agent_belong_to_no_project() -> None:
    assert (OPERATOR.id, MAIN_AGENT.id) == ("operator", "main-agent")
    assert OPERATOR.side is None
    assert not OPERATOR.is_instance
    with pytest.raises(ValueError, match="no project"):
        Principal("operator", PrincipalKind.OPERATOR, PROJECT)
    with pytest.raises(ValueError, match="their kind"):
        Principal("someone", PrincipalKind.MAIN_AGENT)


def test_an_instance_needs_a_project_and_an_exact_identifier() -> None:
    with pytest.raises(ValueError, match="belongs to a project"):
        Principal("owner:x", PrincipalKind.OWNER)
    with pytest.raises(ValueError, match="belongs to a project"):
        Principal("owner:p_x", PrincipalKind.OWNER, "p_x")
    with pytest.raises(ValueError, match="names its side"):
        Principal("contractor:" + PROJECT, PrincipalKind.OWNER, PROJECT)
    with pytest.raises(ValueError, match="lower-case"):
        Principal(f"owner:{PROJECT}:Bad", PrincipalKind.OWNER, PROJECT, "Bad")


@pytest.mark.parametrize("text", ["p_abcdef", "p_abcdef12", "p_" + "a1" * 16])
def test_project_identifiers(text: str) -> None:
    assert is_project_id(text)


@pytest.mark.parametrize(
    "text", ["", "abcdef12", "p_ABCDEF12", "p_abc", "p_abcdef12\n", "p_" + "a" * 33]
)
def test_bad_project_identifiers(text: str) -> None:
    assert not is_project_id(text)


@pytest.mark.parametrize(
    "principal",
    [
        OPERATOR,
        MAIN_AGENT,
        instance_principal(Side.OWNER, PROJECT),
        instance_principal(Side.CONTRACTOR, PROJECT, "b2"),
    ],
)
def test_parse_is_the_inverse_of_id(principal: Principal) -> None:
    assert parse_principal(principal.id) == principal


@pytest.mark.parametrize(
    "text", ["", "root", "owner", "owner:x", "sandbox:" + PROJECT, "owner:a:b:c"]
)
def test_parse_refuses_what_the_hub_never_wrote(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011  # reason: several messages, one meaning
        parse_principal(text)
