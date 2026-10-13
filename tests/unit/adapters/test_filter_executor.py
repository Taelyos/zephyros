"""The executor of the container filter, against a fake of the runtime (US-E1-20, EXG-34)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.fake_docker import FakeDocker
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.core.broker import BrokerError, Op, Request
from zephyros.core.containers import (
    Policy,
    Role,
    VolumeKind,
    guard_name,
    labels,
    network_name,
    turn_name,
    volume_name,
)
from zephyros.core.ports.agent_engine import Side

PROJECT = "p_abcdef12"
OTHER = "p_zzzzzz99"
SUBNET = "10.200.0.16/28"
TOKEN = "zph_c_" + "T" * 43
POLICY = Policy(
    guard_image="ghcr.io/example/guard@sha256:" + "a" * 64,
    instance_image="ghcr.io/example/instance@sha256:" + "b" * 64,
    hub_container="zephyros-hub",
    hub_port=8080,
    model="zephyros-default",
    cpus=2.0,
    memory_mib=1024,
    pids=256,
    guard_memory_mib=32,
    network_pool="10.200.0.0/16",
    network_prefix=28,
)

HERMES_TAIL = ["--oneshot", "--continue=zephyros", "--create-if-missing", "--quiet"]


class World:
    def __init__(self) -> None:
        self.docker = FakeDocker()
        self.docker.add_hub()
        self.journal = MemoryJournal()
        self.executor = Executor(self.docker.api(), POLICY, self.journal, clock=lambda: 1_000_500)

    def do(self, op: Op, **fields: Any) -> dict[str, Any]:
        return self.executor.perform(Request(op, **fields))

    def up(self, project: str = PROJECT, side: Side = Side.CONTRACTOR) -> None:
        self.do(Op.NETWORK_CREATE, project_id=project, side=side, subnet=SUBNET)
        self.do(Op.HUB_CONNECT, project_id=project, side=side)
        for kind in VolumeKind:
            self.do(Op.VOLUME_CREATE, project_id=project, side=side, volume=kind)
        self.do(Op.GUARD_START, project_id=project, side=side, subnet=SUBNET)


@pytest.fixture
def world() -> World:
    return World()


def code_of(world: World, op: Op, **fields: Any) -> str:
    with pytest.raises(BrokerError) as caught:
        world.do(op, **fields)
    return caught.value.code


# ----------------------------------------------------------------------------- the lifecycle
def test_an_instance_comes_up_in_order_and_a_turn_runs_in_the_guards_namespace(
    world: World,
) -> None:
    world.up()
    docker = world.docker
    network = docker.networks[network_name(PROJECT, Side.CONTRACTOR)]
    assert network["Labels"]["org.zephyros.managed"] == "true"
    guard = docker.containers[guard_name(PROJECT, Side.CONTRACTOR)]
    assert guard["State"]["Running"]
    assert guard["NetworkSettings"]["Networks"][network["Name"]]["IPAddress"] == "10.200.0.18"
    assert set(docker.volumes) == {
        volume_name(PROJECT, Side.CONTRACTOR, VolumeKind.HOME),
        volume_name(PROJECT, Side.CONTRACTOR, VolumeKind.WORK),
    }
    hub_address = world.do(Op.HUB_CONNECT, project_id=PROJECT, side=Side.CONTRACTOR)["address"]
    assert hub_address == "10.200.0.19"
    world.do(Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="Work.")
    turn = docker.containers[turn_name(PROJECT, Side.CONTRACTOR)]
    assert turn["HostConfig"]["NetworkMode"] == f"container:{guard_name(PROJECT, Side.CONTRACTOR)}"
    assert turn["Config"]["Cmd"] == ["chat", "--query=Work.", *HERMES_TAIL]
    assert f"ZEPHYROS_HUB_URL=http://{hub_address}:8080" in turn["Config"]["Env"]
    assert turn["State"]["Running"]


def test_the_hub_is_attached_at_its_own_fixed_address_whatever_the_order(world: World) -> None:
    """Neither the hub nor the guard may take the other's address by being attached first."""
    world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
    world.do(Op.HUB_CONNECT, project_id=PROJECT, side=Side.OWNER)
    connects = [c for c in world.docker.calls if c.path.endswith("/connect")]
    assert connects[-1].body["EndpointConfig"] == {"IPAMConfig": {"IPv4Address": "10.200.0.19"}}


def test_the_hub_connection_is_idempotent_and_reversible(world: World) -> None:
    world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
    first = world.do(Op.HUB_CONNECT, project_id=PROJECT, side=Side.OWNER)
    assert world.do(Op.HUB_CONNECT, project_id=PROJECT, side=Side.OWNER) == first
    assert world.do(Op.HUB_DISCONNECT, project_id=PROJECT, side=Side.OWNER) == {
        "was_connected": True
    }
    assert world.do(Op.HUB_DISCONNECT, project_id=PROJECT, side=Side.OWNER) == {
        "was_connected": False
    }


def test_everything_comes_down_and_a_second_stop_is_harmless(world: World) -> None:
    world.up()
    world.do(Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="Work.")
    assert world.do(Op.TURN_STOP, project_id=PROJECT, side=Side.CONTRACTOR) == {"was_present": True}
    assert world.do(Op.TURN_STOP, project_id=PROJECT, side=Side.CONTRACTOR) == {
        "was_present": False
    }
    assert world.do(Op.GUARD_STOP, project_id=PROJECT, side=Side.CONTRACTOR) == {
        "was_present": True
    }
    world.do(Op.HUB_DISCONNECT, project_id=PROJECT, side=Side.CONTRACTOR)
    assert world.do(Op.NETWORK_REMOVE, project_id=PROJECT, side=Side.CONTRACTOR) == {
        "was_present": True
    }
    assert world.do(Op.NETWORK_REMOVE, project_id=PROJECT, side=Side.CONTRACTOR) == {
        "was_present": False
    }
    assert world.do(
        Op.VOLUME_REMOVE, project_id=PROJECT, side=Side.CONTRACTOR, volume=VolumeKind.WORK
    ) == {"was_present": True}
    assert world.do(
        Op.VOLUME_REMOVE, project_id=PROJECT, side=Side.CONTRACTOR, volume=VolumeKind.WORK
    ) == {"was_present": False}
    assert world.do(
        Op.VOLUME_CREATE, project_id=PROJECT, side=Side.CONTRACTOR, volume=VolumeKind.HOME
    ) == {"created": False}


def test_creating_what_exists_is_a_no_op_unless_it_differs(world: World) -> None:
    world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
    assert world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET) == {
        "created": False
    }
    assert (
        code_of(
            world, Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet="10.200.0.32/28"
        )
        == "conflict"
    )


def test_a_turn_needs_a_running_guard_and_a_turn_before_it_must_be_stopped(world: World) -> None:
    world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.CONTRACTOR, subnet=SUBNET)
    world.do(Op.HUB_CONNECT, project_id=PROJECT, side=Side.CONTRACTOR)
    assert (
        code_of(
            world, Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="x"
        )
        == "not_ready"
    )
    world.do(Op.GUARD_START, project_id=PROJECT, side=Side.CONTRACTOR, subnet=SUBNET)
    world.do(Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="x")
    assert (
        code_of(
            world, Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="y"
        )
        == "conflict"
    )
    assert (
        code_of(world, Op.GUARD_START, project_id=PROJECT, side=Side.CONTRACTOR, subnet=SUBNET)
        == "conflict"
    )


def test_the_guard_needs_its_network_the_hub_on_it_and_the_right_subnet(world: World) -> None:
    assert (
        code_of(world, Op.GUARD_START, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
        == "not_found"
    )
    world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
    assert (
        code_of(world, Op.GUARD_START, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
        == "hub_not_connected"
    )
    world.do(Op.HUB_CONNECT, project_id=PROJECT, side=Side.OWNER)
    assert (
        code_of(world, Op.GUARD_START, project_id=PROJECT, side=Side.OWNER, subnet="10.200.0.32/28")
        == "conflict"
    )
    assert code_of(world, Op.HUB_CONNECT, project_id=PROJECT, side=Side.CONTRACTOR) == "not_found"
    assert world.do(Op.HUB_DISCONNECT, project_id=PROJECT, side=Side.CONTRACTOR) == {
        "was_connected": False
    }


def test_a_start_that_fails_leaves_nothing_behind(world: World) -> None:
    world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
    world.do(Op.HUB_CONNECT, project_id=PROJECT, side=Side.OWNER)
    world.docker.fail_start.add(guard_name(PROJECT, Side.OWNER))
    assert (
        code_of(world, Op.GUARD_START, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
        == "runtime_error"
    )
    assert guard_name(PROJECT, Side.OWNER) not in world.docker.containers


# ------------------------------------------------------------------- only Zephyros objects
def test_an_object_that_merely_has_one_of_our_names_is_not_touched(world: World) -> None:
    """The filter recognizes its objects by the labels it set, not by name."""
    world.docker.add_foreign("network", network_name(PROJECT, Side.OWNER))
    world.docker.add_foreign("volume", volume_name(PROJECT, Side.OWNER, VolumeKind.HOME))
    world.docker.add_foreign("container", guard_name(PROJECT, Side.OWNER))
    world.docker.add_foreign("container", turn_name(PROJECT, Side.OWNER))
    before = len(world.docker.mutating_calls())
    attempts: list[tuple[Op, dict[str, Any]]] = [
        (Op.NETWORK_CREATE, {"subnet": SUBNET}),
        (Op.NETWORK_REMOVE, {}),
        (Op.HUB_CONNECT, {}),
        (Op.VOLUME_CREATE, {"volume": VolumeKind.HOME}),
        (Op.VOLUME_REMOVE, {"volume": VolumeKind.HOME}),
        (Op.GUARD_STOP, {}),
        (Op.TURN_STOP, {}),
        (Op.INSPECT, {"role": Role.GUARD}),
        (Op.INSPECT, {"role": Role.TURN}),
    ]
    for op, extra in attempts:
        assert code_of(world, op, project_id=PROJECT, side=Side.OWNER, **extra) == "not_zephyros", (
            op
        )
    assert len(world.docker.mutating_calls()) == before
    assert guard_name(PROJECT, Side.OWNER) in world.docker.containers


@pytest.mark.parametrize(
    ("label", "wrong"),
    [
        ("org.zephyros.managed", "false"),
        ("org.zephyros.managed", ""),
        ("org.zephyros.role", "turn"),
        ("org.zephyros.role", "hub"),
        ("org.zephyros.project", OTHER),
        ("org.zephyros.side", "contractor"),
    ],
)
def test_every_one_of_the_four_labels_must_be_right_for_an_object_to_be_ours(
    world: World, label: str, wrong: str
) -> None:
    """One wrong label among four right ones is enough to leave the object alone."""
    stops = {
        Role.GUARD: (guard_name(PROJECT, Side.OWNER), Op.GUARD_STOP),
        Role.TURN: (turn_name(PROJECT, Side.OWNER), Op.TURN_STOP),
    }
    for role, (name, op) in stops.items():
        right = labels(role, PROJECT, Side.OWNER)
        if right[label] == wrong:
            continue  # this role's own label: the object would be ours
        world.docker.add_foreign("container", name, {**right, label: wrong})
        before = len(world.docker.mutating_calls())
        assert code_of(world, op, project_id=PROJECT, side=Side.OWNER) == "not_zephyros"
        assert len(world.docker.mutating_calls()) == before
        assert name in world.docker.containers


def test_a_labelled_object_of_another_instance_is_not_touched_either(world: World) -> None:
    world.up(PROJECT, Side.OWNER)
    labels = world.docker.containers[guard_name(PROJECT, Side.OWNER)]["Config"]["Labels"]
    world.docker.containers[guard_name(PROJECT, Side.CONTRACTOR)] = world.docker.containers[
        guard_name(PROJECT, Side.OWNER)
    ]
    assert labels["org.zephyros.side"] == "owner"
    assert code_of(world, Op.GUARD_STOP, project_id=PROJECT, side=Side.CONTRACTOR) == "not_zephyros"


def test_the_hub_container_must_be_marked_as_the_hub() -> None:
    world = World()
    world.docker.containers.pop("zephyros-hub")
    world.docker.add_hub(managed=False)
    world.do(Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet=SUBNET)
    assert code_of(world, Op.HUB_CONNECT, project_id=PROJECT, side=Side.OWNER) == "hub_missing"
    world.docker.containers.pop("zephyros-hub")
    assert code_of(world, Op.HUB_CONNECT, project_id=PROJECT, side=Side.OWNER) == "hub_missing"


# ----------------------------------------------------------------------------- reads
def test_inspect_list_and_events_speak_of_zephyros_containers_only(world: World) -> None:
    world.up()
    world.docker.add_foreign("container", "somebody-elses-database", {"app": "db"})
    world.docker.add_foreign(
        "container", "zs-guard-p_foreign1-owner", {"org.zephyros.role": "guard"}
    )
    info = world.do(Op.INSPECT, project_id=PROJECT, side=Side.CONTRACTOR, role=Role.GUARD)[
        "container"
    ]
    assert (info["state"], info["health"], info["address"]) == ("running", "healthy", "10.200.0.18")
    assert world.do(Op.INSPECT, project_id=PROJECT, side=Side.CONTRACTOR, role=Role.TURN) == {
        "container": None
    }
    listed = world.do(Op.LIST)["containers"]
    assert [(c["role"], c["project_id"], c["side"]) for c in listed] == [
        ("guard", PROJECT, "contractor")
    ]
    world.do(Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="Work.")
    world.docker.finish(turn_name(PROJECT, Side.CONTRACTOR), 3)
    exited = world.do(Op.INSPECT, project_id=PROJECT, side=Side.CONTRACTOR, role=Role.TURN)[
        "container"
    ]
    assert (exited["state"], exited["exit_code"]) == ("exited", 3)
    events = world.do(Op.EVENTS, since=0)["events"]
    assert {(e["action"], e["role"]) for e in events} >= {
        ("start", "guard"),
        ("start", "turn"),
        ("die", "turn"),
    }
    assert all(e["project_id"] == PROJECT for e in events)
    assert world.do(Op.EVENTS, since=0)["now"] == 1_000_500


# ----------------------------------------------------------------------------- errors and journal
def test_a_runtime_error_is_reported_without_the_token_or_the_prompt(world: World) -> None:
    world.up()
    world.docker.fail_next["POST /containers/create"] = 1
    with pytest.raises(BrokerError) as caught:
        world.do(
            Op.TURN_START,
            project_id=PROJECT,
            side=Side.CONTRACTOR,
            token=TOKEN,
            prompt="secret prompt",
        )
    assert caught.value.code == "runtime_error"
    assert TOKEN not in str(caught.value)
    assert "secret prompt" not in str(caught.value)
    assert turn_name(PROJECT, Side.CONTRACTOR) not in world.docker.containers


def test_the_journal_records_each_request_without_a_token_or_a_prompt(world: World) -> None:
    world.up()
    world.do(
        Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="top secret"
    )
    code_of(
        world, Op.TURN_START, project_id=PROJECT, side=Side.CONTRACTOR, token=TOKEN, prompt="again"
    )
    outcomes = [(line["outcome"], line["op"], line["code"]) for line in world.journal.lines]
    assert ("done", "turn.start", "ok") in outcomes
    assert ("refused", "turn.start", "conflict") in outcomes
    text = str(world.journal.lines)
    assert TOKEN not in text
    assert "top secret" not in text
    assert PROJECT in text


def test_a_policy_violation_is_a_refusal_not_a_crash(world: World) -> None:
    assert (
        code_of(
            world, Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet="172.17.0.0/28"
        )
        == "policy"
    )
    assert (
        code_of(
            world, Op.NETWORK_CREATE, project_id=PROJECT, side=Side.OWNER, subnet="10.200.0.17/28"
        )
        == "policy"
    )
    refused = [(line["outcome"], line["op"], line["code"]) for line in world.journal.lines]
    assert ("refused", "network.create", "policy") in refused
