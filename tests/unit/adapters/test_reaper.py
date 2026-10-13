"""The reaper: a turn past the configured limit is stopped even when the hub is gone
(build log, decision 22; EXG-21's neighbourhood: a turn must not run forever)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from tests.support.fake_docker import FakeDocker
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.adapters.filter.reaper import Reaper
from zephyros.core.broker import BrokerError, Op, Request
from zephyros.core.containers import (
    Policy,
    VolumeKind,
    guard_name,
    turn_name,
)
from zephyros.core.ports.agent_engine import Side

PROJECT = "p_abcdef12"
SUBNET = "10.200.0.16/28"
TOKEN = "zph_c_" + "T" * 43
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


class _BrokenExecutor:
    """A runtime that is gone: every operation fails, as when the socket is dead."""

    def perform(self, _request: Request) -> dict[str, Any]:
        raise BrokerError("failed", "the runtime is gone")


class _RudeExecutor:
    """A runtime that refuses to stop turns but still lists them."""

    def __init__(self, inner: Executor) -> None:
        self._inner = inner

    def perform(self, request: Request) -> dict[str, Any]:
        if request.op is Op.TURN_STOP:
            raise BrokerError("refused", "the runtime refuses")
        return self._inner.perform(request)


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


class World:
    def __init__(self) -> None:
        self.docker = FakeDocker()
        self.docker.add_hub()
        self.journal = MemoryJournal()
        self.executor = Executor(self.docker.api(), POLICY, self.journal)

    def do(self, op: Op, **fields: object) -> dict[str, object]:
        return self.executor.perform(Request(op, **fields))  # type: ignore[arg-type]  # reason: test fields

    def start_turn(self, side: Side = Side.CONTRACTOR) -> str:
        self.do(Op.NETWORK_CREATE, project_id=PROJECT, side=side, subnet=SUBNET)
        self.do(Op.HUB_CONNECT, project_id=PROJECT, side=side)
        for kind in VolumeKind:
            self.do(Op.VOLUME_CREATE, project_id=PROJECT, side=side, volume=kind)
        self.do(Op.GUARD_START, project_id=PROJECT, side=side, subnet=SUBNET)
        self.do(Op.TURN_START, project_id=PROJECT, side=side, token=TOKEN, prompt="Work.")
        return turn_name(PROJECT, side)


def reaper(world: World, max_age: int = 3600, interval: int = 30, grace: int = 0) -> Reaper:
    return Reaper(
        world.executor,
        max_age,
        grace_seconds=grace,
        interval_seconds=interval,
        now=lambda: NOW,
    )


def test_a_turn_past_its_limit_is_stopped_and_removed_and_journaled() -> None:
    world = World()
    name = world.start_turn()
    world.docker.set_started(name, NOW - timedelta(seconds=3601))
    assert reaper(world).sweep() == [f"{PROJECT}/{Side.CONTRACTOR.value}"]
    assert name not in world.docker.containers
    assert world.journal.lines[-1]["op"] == "turn.stop"
    assert world.journal.lines[-1]["outcome"] == "done"


def test_the_grace_lets_a_live_hub_decide_first() -> None:
    world = World()
    name = world.start_turn()
    world.docker.set_started(name, NOW - timedelta(seconds=3600 + 10))
    assert reaper(world, grace=15).sweep() == []  # one poll of grace is not enough
    assert name in world.docker.containers
    world.docker.set_started(name, NOW - timedelta(seconds=3600 + 16))
    assert reaper(world, grace=15).sweep() == [f"{PROJECT}/{Side.CONTRACTOR.value}"]
    assert name not in world.docker.containers


def test_a_turn_within_its_limit_is_left_alone() -> None:
    world = World()
    name = world.start_turn()
    world.docker.set_started(name, NOW - timedelta(seconds=3599))
    assert reaper(world).sweep() == []
    assert name in world.docker.containers


def test_a_guard_is_never_touched_however_old() -> None:
    world = World()
    world.start_turn()
    guard = guard_name(PROJECT, Side.CONTRACTOR)
    world.docker.set_started(guard, NOW - timedelta(days=30))
    assert reaper(world).sweep() == []
    assert guard in world.docker.containers


def test_a_turn_whose_start_time_cannot_be_read_is_left_alone() -> None:
    world = World()
    name = world.start_turn()
    world.docker.containers[name]["State"]["StartedAt"] = ""
    assert reaper(world).sweep() == []
    assert name in world.docker.containers


def test_an_empty_runtime_is_no_trouble() -> None:
    world = World()
    assert reaper(world).sweep() == []


def test_run_sweeps_then_exits_when_asked_to_stop() -> None:
    async def scenario() -> None:
        world = World()
        name = world.start_turn()
        world.docker.set_started(name, NOW - timedelta(hours=2))
        stop = asyncio.Event()
        task = asyncio.create_task(reaper(world, interval=3600).run(stop))
        for _ in range(100):
            if name not in world.docker.containers:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        assert name not in world.docker.containers

    asyncio.run(scenario())


def test_a_sweep_survives_a_runtime_that_is_gone() -> None:
    reaper_ = Reaper(cast("Executor", _BrokenExecutor()), 3600, now=lambda: NOW)
    assert reaper_.sweep() == []  # the next sweep tries again; the executor journals each refusal


def test_a_turn_that_cannot_be_stopped_does_not_stop_the_sweep() -> None:
    world = World()
    name = world.start_turn()
    world.docker.set_started(name, NOW - timedelta(hours=2))
    reaper_ = Reaper(cast("Executor", _RudeExecutor(world.executor)), 3600, now=lambda: NOW)
    assert reaper_.sweep() == []
    assert name in world.docker.containers  # still there, and the sweep did not raise
