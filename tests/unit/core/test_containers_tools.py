"""ADR 0030: the tools of the projects in the templates and the allow-list of the filter. They are
the one bind of a host path the filter makes: read-only, not recursive, in every turn of both
sides and in every sandbox, and nowhere else."""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import replace
from typing import Any

import pytest

from tests.unit.core.test_containers import PROJECT, SANDBOX_POLICY, TOKEN
from zephyros.adapters.hermes.engine import HermesEngine
from zephyros.adapters.opencode.engine import OpenCodeEngine
from zephyros.core.config import CONFIG_SCHEMA_VERSION, parse_config
from zephyros.core.containers import (
    Policy,
    Role,
    SpecError,
    check_spec,
    guard_spec,
    sandbox_spec,
    tools_mounts,
    turn_spec,
)
from zephyros.core.ports.agent_engine import Side
from zephyros.core.tools import TOOLS_PATH

SOURCE = "/srv/zephyros-tools"
TOOLS = replace(
    SANDBOX_POLICY,
    tools_source=SOURCE,
    contractor_engine="opencode",
    opencode_image="ghcr.io/example/app@sha256:" + "d" * 64,
)
MOUNT = {
    "Type": "bind",
    "Source": SOURCE,
    "Target": TOOLS_PATH,
    "ReadOnly": True,
    "BindOptions": {"Propagation": "rprivate", "NonRecursive": True},
}


def turn(policy: Policy, side: Side) -> dict[str, Any]:
    engine = OpenCodeEngine() if policy.engine(side) == "opencode" else HermesEngine()
    return turn_spec(policy, engine, PROJECT, side, "10.200.0.19", TOKEN, "Do it.")


def sandbox(policy: Policy) -> dict[str, Any]:
    return sandbox_spec(policy, PROJECT, "0a1b2c3d", "tests")


def mounts(spec: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = spec["HostConfig"]["Mounts"]
    return found


def test_the_configuration_gives_the_filter_its_tools() -> None:
    config = parse_config(
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "images": {"instance": "sha256:" + "a" * 64, "guard": "sha256:" + "b" * 64},
            "tools": {"source": SOURCE},
        }
    )
    assert Policy.from_config(config).tools_source == SOURCE
    assert tools_mounts(Policy.from_config(config)) == [MOUNT]


@pytest.mark.parametrize("side", list(Side))
def test_every_turn_of_both_sides_mounts_the_tools_read_only_and_not_recursively(
    side: Side,
) -> None:
    spec = turn(TOOLS, side)
    assert mounts(spec)[-1] == MOUNT
    assert len(mounts(spec)) == 3
    check_spec(spec, Role.TURN, TOOLS)


def test_every_sandbox_mounts_the_same_tools_at_the_same_path() -> None:
    spec = sandbox(TOOLS)
    assert mounts(spec)[-1] == MOUNT
    assert mounts(spec)[-1] == mounts(turn(TOOLS, Side.CONTRACTOR))[-1]
    check_spec(spec, Role.SANDBOX, TOOLS)


def test_without_tools_nothing_is_bound() -> None:
    assert tools_mounts(SANDBOX_POLICY) == []
    assert all(m["Type"] == "volume" for m in mounts(sandbox(SANDBOX_POLICY)))
    assert all(m["Type"] == "volume" for m in mounts(turn(SANDBOX_POLICY, Side.OWNER)))


def test_a_guard_mounts_no_tools() -> None:

    subnet = ipaddress.IPv4Network("10.200.0.16/28")
    spec = guard_spec(TOOLS, PROJECT, Side.OWNER, subnet, "10.200.0.19")
    assert "Mounts" not in spec["HostConfig"]
    spec["HostConfig"]["Mounts"] = [copy.deepcopy(MOUNT)]
    with pytest.raises(SpecError, match="a guard mounts nothing"):
        check_spec(spec, Role.GUARD, TOOLS)


@pytest.mark.parametrize("make", [lambda: turn(TOOLS, Side.OWNER), lambda: sandbox(TOOLS)])
def test_a_turn_or_a_sandbox_without_the_configured_tools_is_refused(make: Any) -> None:
    """The gate cannot run with other tools than the turns, nor a turn without them."""
    spec = make()
    spec["HostConfig"]["Mounts"] = spec["HostConfig"]["Mounts"][:-1]
    role = Role.TURN if spec["Labels"]["org.zephyros.role"] == "turn" else Role.SANDBOX
    with pytest.raises(SpecError, match="mount the tools of the configuration"):
        check_spec(spec, role, TOOLS)


@pytest.mark.parametrize(
    "change",
    [
        {"ReadOnly": False},
        {"Source": "/"},
        {"Source": "/var/run"},
        {"Target": "/work"},
        {"Target": "/opt/project-tools/bin"},
        {"BindOptions": {"Propagation": "rprivate"}},  # recursive: a submount escapes
        {"BindOptions": {"Propagation": "rshared", "NonRecursive": True}},
        {"Type": "volume"},
    ],
)
@pytest.mark.parametrize("role", [Role.TURN, Role.SANDBOX])
def test_a_bind_that_is_not_exactly_the_template_is_refused(
    change: dict[str, Any], role: Role
) -> None:
    spec = turn(TOOLS, Side.CONTRACTOR) if role is Role.TURN else sandbox(TOOLS)
    spec["HostConfig"]["Mounts"][-1] = {**MOUNT, **change}
    with pytest.raises(SpecError):
        check_spec(spec, role, TOOLS)


@pytest.mark.parametrize("role", [Role.TURN, Role.SANDBOX])
def test_a_second_bind_of_the_tools_is_refused(role: Role) -> None:
    spec = turn(TOOLS, Side.CONTRACTOR) if role is Role.TURN else sandbox(TOOLS)
    spec["HostConfig"]["Mounts"].append(copy.deepcopy(MOUNT))
    with pytest.raises(SpecError):
        check_spec(spec, role, TOOLS)


@pytest.mark.parametrize("role", [Role.TURN, Role.SANDBOX])
def test_a_bind_is_refused_when_the_configuration_names_no_tools(role: Role) -> None:
    spec = turn(TOOLS, Side.OWNER) if role is Role.TURN else sandbox(TOOLS)
    policy = replace(TOOLS, tools_source=None)
    with pytest.raises(SpecError, match="only Zephyros volumes"):
        check_spec(spec, role, policy)
