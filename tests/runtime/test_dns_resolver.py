"""ATK-41 on a scripted resolver (decision 38): the DNS variants without touching the Internet.

A fake resolver runs on the core network at a known address and answers a fixed script: a name
that always resolves into the protected range, a rebinding attack (public then protected, TTL
0), a CNAME chain into the protected range, and a name whose A is public while its AAAA is
protected. Every connection that follows such a resolution is blocked by the guard, on the
resolved address at connect time, and the listener on the protected side sees nothing; the
public control resolves and is not blocked.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.runtime.rig import Instance, Runtime
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance]

PROTECTED = "10.77.0.5"
PUBLIC = "93.184.216.34"
PROTECTED_V6 = "fd00::5"


@pytest.fixture(scope="module")
def world(images: dict[str, str]) -> Iterator[Runtime]:
    runtime = Runtime(images, fake_dns=True)
    runtime.setup()
    try:
        yield runtime
    finally:
        runtime.teardown()


@pytest.fixture(scope="module")
def one(world: Runtime) -> Instance:
    return world.bring_up(world.project(), Side.CONTRACTOR)


@pytest.fixture(scope="module")
def lan(world: Runtime, one: Instance) -> str:
    return world.lan(one)


def resolve(world: Runtime, one: Instance, name: str) -> str:
    """What the instance's resolver chain answers for `name` (the scripted resolver)."""
    _code, out = world.sh(one.turn, f"getent hosts {name} | awk '{{print $1}}'")
    return out.strip()


def test_the_instance_resolves_through_the_scripted_resolver(world: Runtime, one: Instance) -> None:
    assert resolve(world, one, "public.test") == PUBLIC


def test_a_name_that_resolves_into_the_protected_range_is_blocked(
    world: Runtime, one: Instance, lan: str
) -> None:
    assert resolve(world, one, "private.test") == PROTECTED
    before = len(world.hits(lan))
    assert world.reach(one.turn, "tcp", "private.test", 80) == "blocked"
    assert len(world.hits(lan)) == before


def test_a_rebinding_attack_is_blocked_on_the_second_answer(
    world: Runtime, one: Instance, lan: str
) -> None:
    """TTL 0: the first A answer is public, the next one protected. The guard judges the
    address at connect time, so the connection that follows the second answer is blocked."""
    assert resolve(world, one, "rebind.test") == PUBLIC
    before = len(world.hits(lan))
    assert world.reach(one.turn, "tcp", "rebind.test", 80) == "blocked"
    assert len(world.hits(lan)) == before


def test_a_cname_chain_into_the_protected_range_is_blocked(
    world: Runtime, one: Instance, lan: str
) -> None:
    assert resolve(world, one, "cname.test") == PROTECTED
    before = len(world.hits(lan))
    assert world.reach(one.turn, "tcp", "cname.test", 80) == "blocked"
    assert len(world.hits(lan)) == before


def test_a_protected_aaaa_is_blocked_when_the_a_is_public(
    world: Runtime, one: Instance, lan: str
) -> None:
    _code, v4 = world.sh(one.turn, "getent ahostsv4 mixed.test | awk '{print $1}' | head -1")
    assert v4.strip() == PUBLIC
    _code, v6 = world.sh(one.turn, "getent ahostsv6 mixed.test | awk '{print $1}' | head -1")
    assert v6.strip() == PROTECTED_V6
    before = len(world.hits(lan))
    assert world.reach(one.turn, "tcp", PROTECTED_V6, 80) == "blocked"
    assert len(world.hits(lan)) == before


def test_the_public_control_is_not_blocked(world: Runtime, one: Instance) -> None:
    """A non-protected destination is not blocked by the guard: the hub endpoint answers
    (the scripted public address itself is not routed from this bench, as an offline host
    would have it)."""
    from tests.runtime.rig import HUB_PORT

    assert world.reach(one.turn, "tcp", one.hub_ip, HUB_PORT) != "blocked"
