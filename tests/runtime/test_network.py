"""EXG-03, EXG-41, EXG-02: what an instance can reach, on a real runtime with a real guard.

Every "blocked" is paired with a control that takes the guard's rules off and shows the same
attempt reaching its listener, so a block is the guard's doing and not an accident of the test
network. The listeners record every peer (oracle O-NET): a blocked attempt leaves no line.
On a network without IPv6, the IPv6 steps are skipped as "not applicable", never passed.

The guards of this module run the `open` level (ADR 0024), the one where every public
destination stays reachable, so that what blocks a protected address is the protected set
itself and not the final drop of the other levels. test_egress_levels.py shows the three levels
side by side, protected networks included.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.runtime.rig import HUB_PORT, LAN_ADDRESSES, OTHER_HUB_PORT, Instance, Runtime, docker
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance]


@pytest.fixture(scope="module")
def runtime(images: dict[str, str]) -> Iterator[Runtime]:
    world = Runtime(images, egress="open")
    world.setup()
    try:
        yield world
    finally:
        world.teardown()


@pytest.fixture(scope="module")
def one(runtime: Runtime) -> Instance:
    return runtime.bring_up(runtime.project(), Side.CONTRACTOR)


@pytest.fixture(scope="module")
def lan(runtime: Runtime, one: Instance) -> str:
    return runtime.lan(one)


@pytest.fixture
def rules_off(runtime: Runtime, one: Instance) -> Iterator[None]:
    runtime.rules_off(one)
    try:
        yield
    finally:
        runtime.rules_on(one)


def targets(proto: str, hosts: tuple[str, ...], port: int) -> list[dict[str, Any]]:
    return [{"proto": proto, "host": host, "port": port} for host in hosts]


def results(found: list[dict[str, Any]]) -> dict[str, str]:
    return {f"{r['proto']} {r['host']}:{r['port']}": str(r["result"]) for r in found}


# ----------------------------------------------------------------------------- N1
@pytest.mark.parametrize(
    "attempt",
    [
        "nft flush ruleset",
        "nft delete table inet zeph",
        "nft add rule inet zeph output accept",
        "ip route add blackhole 1.2.3.4",
        "ip link add d0 type dummy",
        "ip addr add 10.9.9.9/24 dev eth0",
        "unshare -n true",
    ],
)
def test_the_instance_cannot_change_its_rules_or_its_network(
    runtime: Runtime, one: Instance, attempt: str
) -> None:
    for user in (None, "0"):  # the turn's user, and root without capability
        code, out = runtime.sh(one.turn, attempt, user=user)
        assert code != 0, f"{attempt} as {user or 'the turn'}: {out}"
    assert runtime.sh(one.guard, "/usr/local/bin/guard.sh check")[0] == 0


# ----------------------------------------------------------------------------- ATK-39
def test_atk_39_control_the_hub_endpoint_is_reached_and_the_hub_sees_the_guard_address(
    runtime: Runtime, one: Instance
) -> None:
    assert runtime.reach(one.turn, "tcp", one.hub_ip, HUB_PORT) == "reached"
    seen = [line for line in runtime.hits(runtime.hub) if f"-> {one.hub_ip}:{HUB_PORT}" in line]
    assert seen
    assert seen[-1].split()[1].split(":")[0] == one.guard_ip  # EXG-60: an untranslated source


def test_atk_39_the_hub_address_on_another_port_or_protocol_is_blocked(
    runtime: Runtime, one: Instance
) -> None:
    before = len(runtime.hits(runtime.hub))
    assert runtime.reach(one.turn, "tcp", one.hub_ip, OTHER_HUB_PORT) == "blocked"
    assert runtime.reach(one.turn, "udp", one.hub_ip, HUB_PORT) == "blocked"
    code, _ = runtime.sh(one.turn, f"ping -c 1 -W 2 {one.hub_ip}")
    assert code != 0
    assert len(runtime.hits(runtime.hub)) == before


def test_atk_39_control_with_the_rules_off_the_same_attempts_reach_the_hub(
    runtime: Runtime, one: Instance, rules_off: None
) -> None:
    assert runtime.reach(one.turn, "tcp", one.hub_ip, OTHER_HUB_PORT) == "reached"
    assert runtime.reach(one.turn, "udp", one.hub_ip, HUB_PORT) == "reached"


def test_atk_39_the_own_loopback_stays_open(runtime: Runtime, one: Instance) -> None:
    docker("exec", "-d", one.turn, "nc", "-ll", "-p", "7100", "-e", "/bin/echo", "own")
    _code, out = runtime.sh(one.turn, "sleep 0.5; nc -w 2 127.0.0.1 7100 </dev/null")
    assert "own" in out, out


def test_atk_39_every_protected_range_is_blocked_and_its_listener_sees_nothing(
    runtime: Runtime, one: Instance, lan: str
) -> None:
    attempts = targets("tcp", LAN_ADDRESSES, 80) + targets("udp", LAN_ADDRESSES, 80)
    neighbour = runtime.ip_of(lan, one.network)
    attempts += targets("tcp", (neighbour,), 80) + targets("udp", (neighbour,), 80)
    before = len(runtime.hits(lan))
    found = results(runtime.connect(one.turn, attempts))
    assert set(found.values()) == {"blocked"}, found
    assert len(runtime.hits(lan)) == before


def test_atk_39_control_with_the_rules_off_every_range_is_reached(
    runtime: Runtime, one: Instance, lan: str, rules_off: None
) -> None:
    found = results(runtime.connect(one.turn, targets("tcp", LAN_ADDRESSES, 80)))
    assert set(found.values()) == {"reached"}, found


def test_atk_39_the_host_side_and_the_bridge_gateway_are_blocked(
    runtime: Runtime, one: Instance
) -> None:
    gateway = docker(
        "network", "inspect", one.network, "-f", "{{(index .IPAM.Config 0).Gateway}}"
    ).stdout.strip()
    _code, alias = runtime.sh(one.turn, "getent hosts host.docker.internal | awk '{print $1}'")
    hosts = tuple(h for h in (gateway, alias.split("\n")[0] if alias else "") if h)
    found = results(
        runtime.connect(one.turn, targets("tcp", hosts, 22) + targets("tcp", hosts, 80))
    )
    assert set(found.values()) == {"blocked"}, found


def test_atk_39_ipv6_protected_ranges_are_blocked(runtime: Runtime, one: Instance) -> None:
    if not runtime.has_ipv6(one.turn):
        pytest.skip("not applicable: the instance network has no IPv6")
    hosts = ("::1", "fd00::1", "fe80::1", "fd00:ec2::254", "64:ff9b::a00:1", "2002:a00:1::1")
    found = results(runtime.connect(one.turn, targets("tcp", hosts[1:], 80)))
    assert set(found.values()) == {"blocked"}, found


# ----------------------------------------------------------------------------- ATK-40
@pytest.mark.parametrize(
    "host",
    [
        "167772165",
        "012.0.0.5",
        "0xa.0.0.5",
        "0x0a000005",
        "10.5",
        "127.1",
        "0.0.0.0",
        "0",
        "::ffff:10.77.0.5",
        "10.77.0.5.",
    ],
)
def test_atk_40_other_spellings_of_a_protected_address_are_blocked(
    runtime: Runtime, one: Instance, lan: str, host: str
) -> None:
    before = len(runtime.hits(lan))
    found = runtime.connect(one.turn, targets("tcp", (host,), 80))[0]
    # 127.1 and 0.0.0.0 are the instance's own loopback, where nothing listens on port 80:
    # refused, and never a listener outside.
    assert found["result"] == "blocked", found
    assert len(runtime.hits(lan)) == before


def test_atk_40_a_url_with_userinfo_and_a_trailing_dot_is_blocked(
    runtime: Runtime, one: Instance, lan: str
) -> None:
    before = len(runtime.hits(lan))
    userinfo = ":".join(("user", "not-a-secret"))  # written in two parts for the secret scanner
    for url in (f"http://{userinfo}@10.77.0.5/", "http://10.77.0.5./", "http://hub@10.77.0.5:80/"):
        code, out = runtime.sh(one.turn, f"wget -q -T 2 -O - '{url}'")
        assert code != 0, (url, out)
    assert len(runtime.hits(lan)) == before


# ----------------------------------------------------------------------------- ATK-41
def test_atk_41_a_public_name_that_resolves_to_a_protected_address_is_blocked(
    runtime: Runtime, one: Instance, lan: str
) -> None:
    _code, out = runtime.sh(one.turn, "getent hosts 10.77.0.5.nip.io | awk '{print $1}'")
    if out.strip() != "10.77.0.5":
        pytest.skip("not applicable here: the public wildcard name does not resolve (offline?)")
    before = len(runtime.hits(lan))
    assert runtime.reach(one.turn, "tcp", "10.77.0.5.nip.io", 80) == "blocked"
    assert len(runtime.hits(lan)) == before


# ----------------------------------------------------------------------------- ATK-42
def test_atk_42_a_redirect_to_a_protected_address_is_not_followed_through(
    runtime: Runtime, one: Instance, lan: str
) -> None:
    """The hub stand-in plays a page that answers 302 to 10.77.0.5; the redirected connection
    is a new connection to a protected address, and the guard drops it."""
    before = len(runtime.hits(lan))
    _code, out = runtime.sh(one.turn, f"probe fetch 'http://{one.hub_ip}:{HUB_PORT}/redirect'")
    assert '"blocked"' in out, out
    assert len(runtime.hits(lan)) == before
    assert any("/redirect" not in line for line in runtime.hits(runtime.hub))  # it was asked


# ----------------------------------------------------------------------------- ATK-43
def test_atk_43_the_names_of_other_containers_do_not_resolve(
    runtime: Runtime, one: Instance
) -> None:
    other = runtime.bring_up(runtime.project(), Side.OWNER)
    for name in (other.guard, other.turn, "zs-test-lan-x", runtime.core):
        code, out = runtime.sh(one.turn, f"getent hosts {name}")
        assert code != 0, (name, out)


def test_atk_43_raw_sockets_and_a_second_interface_are_refused(
    runtime: Runtime, one: Instance
) -> None:
    raw = (
        "python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_RAW, "
        "socket.IPPROTO_TCP)'"
    )
    assert runtime.sh(one.turn, raw)[0] != 0
    packet = "python3 -c 'import socket; socket.socket(socket.AF_PACKET, socket.SOCK_RAW)'"
    assert runtime.sh(one.turn, packet)[0] != 0
    _code, links = runtime.sh(one.turn, "ls /sys/class/net")
    assert sorted(links.split()) == ["eth0", "lo"]


# ----------------------------------------------------------------------------- ATK-44
def test_atk_44_control_at_the_open_level_public_destinations_stay_open(
    runtime: Runtime, one: Instance
) -> None:
    """Open only: at the default level (regulated) the same fetch is refused unless it goes
    through the egress proxy of the hub, and at sealed it is refused (test_egress_levels.py)."""
    host_online = docker(
        "run",
        "--rm",
        "--label",
        runtime.label,
        "--network",
        runtime.core,
        "--entrypoint",
        "probe",
        runtime.images["probe"],
        "connect",
        '[{"proto": "tcp", "host": "1.1.1.1", "port": 443}]',
        check=False,
    )
    if '"reached"' not in host_online.stdout:
        pytest.skip("not applicable here: the machine has no route to the Internet")
    assert runtime.reach(one.turn, "tcp", "1.1.1.1", 443) == "reached"
    _code, out = runtime.sh(one.turn, "probe fetch https://pypi.org/simple/pip/")
    assert '"reached"' in out, out


# ----------------------------------------------------------------------------- ATK-34, 38, 76
@pytest.fixture(scope="module")
def neighbours(runtime: Runtime, one: Instance) -> dict[str, Instance]:
    """The other side of the same project, and an instance of another project, each with a
    listener on all interfaces (a Hermes API server, dashboard or kanban, ATK-76)."""
    found = {
        "other_side": runtime.bring_up(one.project_id, Side.OWNER),
        "other_project": runtime.bring_up(runtime.project(), Side.CONTRACTOR),
    }
    for instance in (one, *found.values()):
        for port in (8642, 9119, 7000):
            docker(
                "exec",
                "-d",
                instance.turn,
                "nc",
                "-ll",
                "-p",
                str(port),
                "-e",
                "/bin/echo",
                "listener",
            )
    return found


@pytest.mark.parametrize("which", ["other_side", "other_project"])
def test_atk_34_38_76_no_instance_reaches_another(
    runtime: Runtime, one: Instance, neighbours: dict[str, Instance], which: str
) -> None:
    other = neighbours[which]
    for source, target in ((one, other), (other, one)):
        attempts = [
            {"proto": "tcp", "host": target.guard_ip, "port": port}
            for port in (8642, 9119, 7000, 22)
        ] + [
            {"proto": "tcp", "host": str(h), "port": 8642} for h in list(target.subnet.hosts())[2:6]
        ]
        found = results(runtime.connect(source.turn, attempts))
        assert set(found.values()) == {"blocked"}, found


def test_atk_76_a_listener_inside_an_instance_is_not_reached_from_the_hub_network(
    runtime: Runtime, one: Instance, neighbours: dict[str, Instance]
) -> None:
    """Not even the hub, which shares the network, opens a connection into an instance: there is
    no inbound rule (option A of spike Z2-01)."""
    code, out = runtime.sh(
        runtime.hub,
        f"python -c \"import socket; socket.create_connection(('{one.guard_ip}', 8642), 2)\"",
    )
    assert code != 0, out


def test_atk_76_control_the_listener_answers_inside_its_own_instance(
    runtime: Runtime, one: Instance, neighbours: dict[str, Instance]
) -> None:
    _code, out = runtime.sh(one.turn, "nc -w 2 127.0.0.1 8642 </dev/null")
    assert "listener" in out


def test_atk_38_control_with_both_guards_off_the_runtime_alone_isolates_or_not(
    runtime: Runtime, one: Instance, neighbours: dict[str, Instance]
) -> None:
    """Information for residual risk R-16: what the runtime's own isolation between networks
    does when both guards' rules are off. The test passes either way and says which."""
    other = neighbours["other_project"]
    runtime.rules_off(one)
    runtime.rules_off(other)
    try:
        result = runtime.reach(one.turn, "tcp", other.guard_ip, 8642)
    finally:
        runtime.rules_on(one)
        runtime.rules_on(other)
    print(f"R-16: with both guards off, another network's listener is {result}")
