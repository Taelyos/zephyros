"""ADR 0024 on a real runtime with real guards: what a turn reaches at each level.

One world per level, each with its own stand-in of the hub (it listens on the agent endpoint,
on the egress proxy and on a port no instance may reach) and its own stand-in of the local
network. Each attempt goes out from inside a turn, as its user. What the guard of each level
must give:

| Level     | hub agent endpoint | hub egress proxy | public Internet | protected networks |
| --------- | ------------------ | ---------------- | --------------- | ------------------ |
| regulated | reached            | reached          | blocked         | blocked            |
| sealed    | reached            | blocked          | blocked         | blocked            |
| open      | reached            | blocked          | reached         | blocked            |

The public steps need a machine with a route to the Internet; without one they are skipped as
"not applicable", never passed. What the egress proxy itself allows, and what the hub logs, is
shown with the real hub in test_hermes.py.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.runtime.rig import (
    HUB_PORT,
    LAN_ADDRESSES,
    OTHER_HUB_PORT,
    PROXY_PORT,
    Instance,
    Runtime,
    docker,
)
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance]

LEVELS = ("regulated", "sealed", "open")
PUBLIC = ("1.1.1.1", 443)


@pytest.fixture(scope="module")
def worlds(images: dict[str, str]) -> Iterator[dict[str, tuple[Runtime, Instance, str]]]:
    made: dict[str, tuple[Runtime, Instance, str]] = {}
    try:
        for level in LEVELS:
            world = Runtime(images, egress=level)
            world.setup()
            instance = world.bring_up(world.project(), Side.CONTRACTOR)
            made[level] = (world, instance, world.lan(instance))
        yield made
    finally:
        for world, _instance, _lan in made.values():
            world.teardown()


@pytest.fixture(scope="module")
def online(worlds: dict[str, tuple[Runtime, Instance, str]]) -> bool:
    """Whether this machine reaches the Internet at all, seen from outside every guard."""
    world = worlds["open"][0]
    seen = docker(
        "run",
        "--rm",
        "--label",
        world.label,
        "--network",
        world.core,
        "--entrypoint",
        "probe",
        world.images["probe"],
        "connect",
        f'[{{"proto": "tcp", "host": "{PUBLIC[0]}", "port": {PUBLIC[1]}}}]',
        check=False,
    )
    return '"reached"' in seen.stdout


def _hub_hits(world: Runtime, port: int) -> int:
    return len([line for line in world.hits(world.hub) if line.endswith(f":{port}")])


@pytest.mark.parametrize("level", LEVELS)
def test_every_level_reaches_the_agent_endpoint_of_the_hub(
    worlds: dict[str, tuple[Runtime, Instance, str]], level: str
) -> None:
    world, one, _lan = worlds[level]
    before = _hub_hits(world, HUB_PORT)
    assert world.reach(one.turn, "tcp", one.hub_ip, HUB_PORT) == "reached"
    assert _hub_hits(world, HUB_PORT) == before + 1


@pytest.mark.parametrize(
    ("level", "expected"), [("regulated", "reached"), ("sealed", "blocked"), ("open", "blocked")]
)
def test_only_a_regulated_turn_reaches_the_egress_proxy(
    worlds: dict[str, tuple[Runtime, Instance, str]], level: str, expected: str
) -> None:
    world, one, _lan = worlds[level]
    before = _hub_hits(world, PROXY_PORT)
    assert world.reach(one.turn, "tcp", one.hub_ip, PROXY_PORT) == expected
    assert _hub_hits(world, PROXY_PORT) == before + (expected == "reached")
    # and no level opens another port of the hub
    assert world.reach(one.turn, "tcp", one.hub_ip, OTHER_HUB_PORT) == "blocked"


@pytest.mark.parametrize("level", LEVELS)
def test_no_level_reaches_a_protected_network(
    worlds: dict[str, tuple[Runtime, Instance, str]], level: str
) -> None:
    world, one, lan = worlds[level]
    before = len(world.hits(lan))
    attempts = [
        {"proto": proto, "host": host, "port": 80}
        for host in LAN_ADDRESSES
        for proto in ("tcp", "udp")
    ]
    found = {f"{r['proto']} {r['host']}": r["result"] for r in world.connect(one.turn, attempts)}
    assert set(found.values()) == {"blocked"}, found
    assert len(world.hits(lan)) == before


@pytest.mark.parametrize("level", ["regulated", "sealed"])
def test_a_regulated_or_sealed_turn_reaches_no_public_destination_directly(
    worlds: dict[str, tuple[Runtime, Instance, str]], online: bool, level: str
) -> None:
    if not online:
        pytest.skip("not applicable here: the machine has no route to the Internet")
    world, one, _lan = worlds[level]
    assert world.reach(one.turn, "tcp", *PUBLIC) == "blocked"
    # no name leaves either: the public resolver is a public destination like any other
    assert world.reach(one.turn, "udp", "9.9.9.9", 53) == "blocked"
    code, out = world.sh(one.turn, "getent hosts pypi.org")
    assert code != 0, out
    _code, out = world.sh(one.turn, "probe fetch https://pypi.org/simple/pip/")
    assert '"blocked"' in out, out


def test_control_with_the_rules_off_the_same_regulated_turn_reaches_the_internet(
    worlds: dict[str, tuple[Runtime, Instance, str]], online: bool
) -> None:
    if not online:
        pytest.skip("not applicable here: the machine has no route to the Internet")
    world, one, _lan = worlds["regulated"]
    world.rules_off(one)
    try:
        assert world.reach(one.turn, "tcp", *PUBLIC) == "reached"
    finally:
        world.rules_on(one)
    assert world.reach(one.turn, "tcp", *PUBLIC) == "blocked"


def test_an_open_turn_reaches_a_public_destination(
    worlds: dict[str, tuple[Runtime, Instance, str]], online: bool
) -> None:
    if not online:
        pytest.skip("not applicable here: the machine has no route to the Internet")
    world, one, _lan = worlds["open"]
    assert world.reach(one.turn, "tcp", *PUBLIC) == "reached"


@pytest.mark.parametrize("level", ["regulated", "sealed"])
def test_the_guard_is_unhealthy_once_its_final_drop_is_gone(
    worlds: dict[str, tuple[Runtime, Instance, str]], level: str
) -> None:
    """The health check states the level: without its last drop a regulated or sealed guard
    is an open one, and the hub stops an instance whose guard is unhealthy (ADR 0015)."""
    world, one, _lan = worlds[level]
    remove = (
        "h=$(nft -a list chain inet zeph output | grep -E '^\\s*counter packets .* drop # handle'"
        " | sed 's/.*handle //'); nft delete rule inet zeph output handle $h"
    )
    assert world.sh(one.guard, remove)[0] == 0
    try:
        code, out = world.sh(one.guard, "/usr/local/bin/guard.sh check")
        assert code != 0
        assert "does not end with a drop" in out
    finally:
        world.rules_on(one)
    assert world.sh(one.guard, "/usr/local/bin/guard.sh check")[0] == 0


@pytest.mark.parametrize("level", LEVELS)
def test_the_turn_cannot_change_the_level_of_its_guard(
    worlds: dict[str, tuple[Runtime, Instance, str]], level: str
) -> None:
    world, one, _lan = worlds[level]
    for attempt in ("nft delete table inet zeph", "nft add rule inet zeph output accept"):
        for user in (None, "0"):
            code, out = world.sh(one.turn, attempt, user=user)
            assert code != 0, (attempt, user, out)
    assert world.sh(one.guard, "/usr/local/bin/guard.sh check")[0] == 0
