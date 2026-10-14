"""The lifecycle of instances, through the real filter stack and a fake container runtime.

The manager talks to a `FilterClient` whose transport is the filter application itself, in front
of the executor and the fake runtime. Nothing is mocked between the manager and the fake runtime,
so what these tests see is what a runtime would be asked to do.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.support.fake_docker import FakeDocker
from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.adapters.filter.server import build_filter_app
from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.instance_store import InMemoryInstanceStore
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.adapters.memory.token_store import InMemoryTokenStore
from zephyros.core.audit import Action, AuditLog, AuditRecord, Outcome, Reason
from zephyros.core.broker import BrokerError
from zephyros.core.containers import Policy, guard_name, network_name, turn_name, volume_name
from zephyros.core.containers import VolumeKind as Volume
from zephyros.core.continuity import Continuity, TaskState
from zephyros.core.continuity import Timing as ContinuityTiming
from zephyros.core.errors import Refusal
from zephyros.core.instances import InstanceState as State
from zephyros.core.lifecycle import LOST_ROUNDS, InstanceManager, Timing
from zephyros.core.notifications import Notifier
from zephyros.core.ports.agent_engine import Side
from zephyros.core.tokens import Authenticated, Endpoint, Rejected, TokenService
from zephyros.core.webhooks import NotifyResult

OWNER, CONTRACTOR = Side.OWNER, Side.CONTRACTOR
PROJECT = "p_abcdef12"
OTHER = "p_zzzzzz99"
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
TIMING = Timing(
    guard_health_seconds=30,
    poll_seconds=5,
    turn_seconds=600,
    silence_seconds=120,
    token_lifetime_seconds=3600,
    rotate_before_seconds=300,
    pool="10.200.0.0/16",
    prefix=28,
)

HERMES_TAIL = ["--oneshot", "--continue=zephyros", "--create-if-missing", "--quiet"]


class FlakyBroker:
    """The real client, unless told that the filter is out."""

    def __init__(self, inner: FilterClient) -> None:
        self.inner = inner
        self.down = False
        self.hooks: dict[str, Any] = {}
        self.explode: set[str] = set()
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.down:
                raise BrokerError("filter_unreachable", "the filter could not be reached")
            self.calls.append(name)
            if name in self.explode:
                raise RuntimeError("a bug")
            result = method(*args, **kwargs)
            hook = self.hooks.pop(name, None)
            if hook is not None:
                hook()
            return result

        return call


@dataclass
class Rig:
    docker: FakeDocker
    broker: FlakyBroker
    clock: FakeClock
    store: InMemoryInstanceStore
    tokens: TokenService
    audit: AuditLog
    manager: InstanceManager
    notifier: Notifier | None = None

    def make_manager(
        self,
        timing: Timing = TIMING,
        *,
        continuity: Continuity | None = None,
        notify: Any = None,
    ) -> InstanceManager:
        self.notifier = Notifier(self.audit, notify)
        return InstanceManager(
            self.broker,
            self.store,
            self.tokens,
            self.audit,
            self.clock,
            timing,
            sleep=self.clock.advance,
            continuity=continuity,
            notifier=self.notifier,
        )

    # -- driving
    def run_jobs(self) -> None:
        while self.manager.step():
            pass

    def send_notifications(self) -> None:
        """What the notifier's worker does: send what the watchdog queued."""
        assert self.notifier is not None
        while self.notifier.step():
            pass

    def start(self, project: str = PROJECT, side: Side = CONTRACTOR, prompt: str = "Work.") -> None:
        self.manager.start(project, side, prompt)
        self.run_jobs()

    def state(self, project: str = PROJECT, side: Side = CONTRACTOR) -> State:
        return self.manager.status(project, side).state

    # -- observing
    def turn_secret(self, project: str = PROJECT, side: Side = CONTRACTOR) -> str:
        env = self.docker.containers[turn_name(project, side)]["Config"]["Env"]
        return str(next(e.split("=", 1)[1] for e in env if e.startswith("ZEPHYROS_TOKEN=")))

    def records(self, project: str = PROJECT) -> list[AuditRecord]:
        return self.audit.read(project)

    def actions(self, project: str = PROJECT) -> list[tuple[Action, Outcome]]:
        return [(r.action, r.outcome) for r in self.records(project)]

    def who_is(self, secret: str, address: str) -> Any:
        return self.tokens.authenticate(secret, peer_address=address, endpoint=Endpoint.AGENT)

    def nothing_left(self, project: str = PROJECT, side: Side = CONTRACTOR) -> bool:
        return (
            network_name(project, side) not in self.docker.networks
            and guard_name(project, side) not in self.docker.containers
            and turn_name(project, side) not in self.docker.containers
            and self.store.get(project, side) is None
        )


@pytest.fixture
def rig() -> Rig:
    docker = FakeDocker()
    docker.add_hub()
    journal = MemoryJournal()
    executor = Executor(docker.api(), POLICY, journal, clock=lambda: 1_000_000)
    client = FilterClient(TestClient(build_filter_app(executor, journal)))
    broker = FlakyBroker(client)
    clock = FakeClock()
    store = InMemoryInstanceStore()
    tokens = TokenService(InMemoryTokenStore(), clock)
    audit = AuditLog(InMemoryLogStore(), clock)
    made = Rig(docker, broker, clock, store, tokens, audit, None)  # type: ignore[arg-type]  # reason: the manager is built next, from the rig
    made.manager = made.make_manager()
    made.manager.reconcile()
    return made


# ----------------------------------------------------------------------------- start
def test_a_start_is_a_request_that_returns_at_once_and_touches_nothing(rig: Rig) -> None:
    info = rig.manager.start(PROJECT, CONTRACTOR, "Work.")
    assert info.state is State.STARTING
    assert not rig.docker.calls[len(rig.docker.calls) :]  # no call beyond the reconcile's
    assert not rig.docker.networks
    assert rig.manager.status(PROJECT, CONTRACTOR).state is State.STARTING


def test_the_worker_brings_the_instance_up_and_starts_the_first_turn(rig: Rig) -> None:
    rig.start(prompt="Build the thing.")
    assert rig.state() is State.TURN
    docker = rig.docker
    network = docker.networks[network_name(PROJECT, CONTRACTOR)]
    subnet = network["IPAM"]["Config"][0]["Subnet"]
    assert subnet == "10.200.0.0/28"
    guard = docker.containers[guard_name(PROJECT, CONTRACTOR)]
    assert guard["State"]["Health"]["Status"] == "healthy"
    assert {volume_name(PROJECT, CONTRACTOR, v) for v in Volume} == set(docker.volumes)
    turn = docker.containers[turn_name(PROJECT, CONTRACTOR)]
    assert turn["Config"]["Cmd"] == ["chat", "--query=Build the thing.", *HERMES_TAIL]
    info = rig.manager.status(PROJECT, CONTRACTOR)
    assert info.address == "10.200.0.2"
    assert info.turn_started is not None
    assert rig.store.get(PROJECT, CONTRACTOR) is not None


def test_the_token_is_bound_to_the_guard_and_to_nothing_else(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    ok = rig.who_is(secret, "10.200.0.2")
    assert isinstance(ok, Authenticated)
    assert ok.principal.id == f"contractor:{PROJECT}"
    for elsewhere in ("10.200.0.3", "10.200.0.18", "172.18.0.1", "127.0.0.1"):
        refused = rig.who_is(secret, elsewhere)
        assert isinstance(refused, Rejected)
        assert refused.reason is Reason.WRONG_SOURCE


def test_the_lifecycle_is_in_the_log_without_a_secret_or_a_prompt(rig: Rig) -> None:
    rig.start(prompt="a very particular prompt")
    secret = rig.turn_secret()
    kinds = rig.actions()
    assert (Action.TOKEN_ISSUE, Outcome.OK) in kinds
    assert (Action.TURN_START, Outcome.OK) in kinds
    text = " ".join(f"{r.principal} {r.parameters}" for r in rig.records())
    assert secret not in text
    assert "particular" not in text
    assert all(r.principal == "hub" for r in rig.records())
    assert any('"prompt_chars":24' in r.parameters for r in rig.records())


def test_a_second_start_while_busy_is_refused_and_changes_nothing(rig: Rig) -> None:
    rig.manager.start(PROJECT, CONTRACTOR, "one")
    with pytest.raises(Refusal) as caught:
        rig.manager.start(PROJECT, CONTRACTOR, "two")
    assert caught.value.reason is Reason.BUSY
    rig.run_jobs()
    with pytest.raises(Refusal):
        rig.manager.start(PROJECT, CONTRACTOR, "three")
    assert (
        rig.docker.containers[turn_name(PROJECT, CONTRACTOR)]["Config"]["Cmd"][1] == "--query=one"
    )


def test_the_manager_does_not_start_before_it_has_taken_stock(rig: Rig) -> None:
    fresh = rig.make_manager()
    with pytest.raises(Refusal, match="still checking") as caught:
        fresh.start(PROJECT, CONTRACTOR, "x")
    assert caught.value.reason is Reason.BUSY
    rig.broker.down = True
    fresh.tick()  # cannot reconcile; stays closed
    with pytest.raises(Refusal):
        fresh.start(PROJECT, CONTRACTOR, "x")
    rig.broker.down = False
    fresh.tick()
    assert fresh.start(PROJECT, CONTRACTOR, "x").state is State.STARTING


def test_two_sides_and_two_projects_each_get_their_own_network_and_address(rig: Rig) -> None:
    for project in (PROJECT, OTHER):
        for side in (OWNER, CONTRACTOR):
            rig.start(project, side)
    subnets = {
        r.subnet
        for r in (rig.store.get(p, s) for p in (PROJECT, OTHER) for s in (OWNER, CONTRACTOR))
        if r
    }
    assert len(subnets) == 4
    secrets = {(p, s): rig.turn_secret(p, s) for p in (PROJECT, OTHER) for s in (OWNER, CONTRACTOR)}
    for (project, side), secret in secrets.items():
        mine = rig.manager.status(project, side).address
        assert mine is not None
        assert isinstance(rig.who_is(secret, mine), Authenticated)
        for other_key in secrets:
            if other_key != (project, side):
                theirs = rig.manager.status(*other_key).address
                assert theirs != mine
                assert isinstance(rig.who_is(secret, str(theirs)), Rejected)


def test_when_the_pool_is_used_up_the_start_fails_cleanly(rig: Rig) -> None:
    small = replace(TIMING, pool="10.200.0.0/27")
    rig.manager = rig.make_manager(small)
    rig.manager.reconcile()
    rig.start(PROJECT, OWNER)
    rig.start(PROJECT, CONTRACTOR)
    rig.start(OTHER, OWNER)
    info = rig.manager.status(OTHER, OWNER)
    assert info.state is State.ABSENT
    assert info.last_turn == "start_failed:no_free_subnet"
    assert rig.nothing_left(OTHER, OWNER)
    assert rig.state(PROJECT, OWNER) is State.TURN


# ----------------------------------------------------------------------------- start that fails
def test_a_guard_that_never_becomes_healthy_fails_the_start_and_leaves_nothing(rig: Rig) -> None:
    rig.docker.hold_health.add(guard_name(PROJECT, CONTRACTOR))
    before = rig.clock.now()
    rig.start()
    assert rig.state() is State.ABSENT
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "start_failed:guard_timeout"
    assert rig.clock.now() - before >= timedelta(seconds=TIMING.guard_health_seconds)
    assert rig.nothing_left()
    assert set(rig.docker.volumes) == {volume_name(PROJECT, CONTRACTOR, v) for v in Volume}
    assert (Action.TURN_START, Outcome.ERROR) in rig.actions()
    assert (Action.TOKEN_ISSUE, Outcome.OK) not in rig.actions()  # no token before a healthy guard


def test_a_guard_that_reports_unhealthy_fails_the_start_at_once(rig: Rig) -> None:
    rig.broker.hooks["start_guard"] = lambda: rig.docker.kill_health(
        guard_name(PROJECT, CONTRACTOR), "unhealthy"
    )
    before = rig.clock.now()
    rig.start()
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "start_failed:guard_failed"
    assert rig.clock.now() - before < timedelta(seconds=TIMING.guard_health_seconds)
    assert rig.nothing_left()


def test_a_runtime_that_refuses_to_start_the_guard_fails_the_start(rig: Rig) -> None:
    rig.docker.fail_start.add(guard_name(PROJECT, CONTRACTOR))
    rig.start()
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "start_failed:runtime_error"
    assert rig.nothing_left()


def test_a_failed_turn_start_on_a_live_instance_keeps_the_instance(rig: Rig) -> None:
    rig.start()
    rig.docker.finish(turn_name(PROJECT, CONTRACTOR), 0)
    rig.manager.tick()
    rig.docker.fail_start.add(turn_name(PROJECT, CONTRACTOR))
    rig.start()
    info = rig.manager.status(PROJECT, CONTRACTOR)
    assert info.state is State.IDLE
    assert info.last_turn == "start_failed:runtime_error"
    assert guard_name(PROJECT, CONTRACTOR) in rig.docker.containers


def test_a_bug_in_a_job_is_a_failed_start_and_the_worker_goes_on(rig: Rig) -> None:
    rig.broker.explode.add("create_network")
    rig.start()
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "start_failed:internal_error"
    rig.broker.explode.clear()
    rig.start(OTHER, OWNER)
    assert rig.state(OTHER, OWNER) is State.TURN


# ----------------------------------------------------------------------------- turns end
def test_a_turn_that_ends_leaves_an_idle_instance_and_a_reason(rig: Rig) -> None:
    rig.start()
    rig.docker.finish(turn_name(PROJECT, CONTRACTOR), 0)
    assert rig.state() is State.TURN  # until the watchdog looks
    rig.manager.tick()
    info = rig.manager.status(PROJECT, CONTRACTOR)
    assert (info.state, info.last_turn, info.turn_started) == (State.IDLE, "completed", None)
    assert turn_name(PROJECT, CONTRACTOR) not in rig.docker.containers
    assert (Action.TURN_END, Outcome.OK) in rig.actions()


def test_a_turn_that_fails_is_reported_as_failed_and_logged_as_an_error(rig: Rig) -> None:
    rig.start()
    rig.docker.finish(turn_name(PROJECT, CONTRACTOR), 3)
    rig.manager.tick()
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "failed:exit_3"
    assert (Action.TURN_END, Outcome.ERROR) in rig.actions()


def test_a_turn_that_vanishes_is_reported_as_failed(rig: Rig) -> None:
    rig.start()
    del rig.docker.containers[turn_name(PROJECT, CONTRACTOR)]
    rig.manager.tick()
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "failed:vanished"
    assert rig.state() is State.IDLE


def test_a_new_turn_reuses_the_instance_and_the_token(rig: Rig) -> None:
    rig.start(prompt="first")
    secret = rig.turn_secret()
    rig.docker.finish(turn_name(PROJECT, CONTRACTOR), 0)
    rig.manager.tick()
    networks_before = len([c for c in rig.docker.calls if c.path.endswith("/networks/create")])
    rig.start(prompt="second")
    assert rig.state() is State.TURN
    assert rig.turn_secret() == secret
    assert [a for a, _ in rig.actions()].count(Action.TOKEN_ISSUE) == 1
    assert (
        len([c for c in rig.docker.calls if c.path.endswith("/networks/create")]) == networks_before
    )
    assert (
        rig.docker.containers[turn_name(PROJECT, CONTRACTOR)]["Config"]["Cmd"][1]
        == "--query=second"
    )
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn is None


def test_a_turn_that_runs_too_long_is_stopped(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    rig.clock.advance(TIMING.turn_seconds - 10)
    rig.who_is(secret, "10.200.0.2")  # still talking
    rig.manager.tick()
    assert rig.state() is State.TURN
    rig.clock.advance(20)
    rig.who_is(secret, "10.200.0.2")
    rig.manager.tick()
    info = rig.manager.status(PROJECT, CONTRACTOR)
    assert (info.state, info.last_turn) == (State.IDLE, "timeout")
    assert turn_name(PROJECT, CONTRACTOR) not in rig.docker.containers


def test_a_turn_that_no_longer_calls_the_hub_is_stopped(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    rig.clock.advance(TIMING.silence_seconds - 10)
    rig.manager.tick()
    assert rig.state() is State.TURN
    rig.who_is(secret, "10.200.0.2")  # a call: the silence starts again
    rig.clock.advance(TIMING.silence_seconds - 10)
    rig.manager.tick()
    assert rig.state() is State.TURN
    rig.clock.advance(20)
    rig.manager.tick()
    info = rig.manager.status(PROJECT, CONTRACTOR)
    assert (info.state, info.last_turn) == (State.IDLE, "silent")


def test_a_call_from_the_wrong_address_does_not_count_as_a_sign_of_life(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    rig.clock.advance(TIMING.silence_seconds - 10)
    rig.who_is(secret, "10.200.0.99")
    rig.clock.advance(20)
    rig.manager.tick()
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "silent"


# ----------------------------------------------------------------------------- tokens
def test_a_token_near_its_end_is_replaced_at_the_next_turn(rig: Rig) -> None:
    rig.start()
    old = rig.turn_secret()
    rig.docker.finish(turn_name(PROJECT, CONTRACTOR), 0)
    rig.manager.tick()
    rig.clock.advance(TIMING.token_lifetime_seconds - TIMING.rotate_before_seconds + 1)
    rig.start()
    new = rig.turn_secret()
    assert new != old
    assert isinstance(rig.who_is(new, "10.200.0.2"), Authenticated)
    refused = rig.who_is(old, "10.200.0.2")
    assert isinstance(refused, Rejected)
    assert refused.reason is Reason.REVOKED
    kinds = [a for a, _ in rig.actions()]
    assert kinds.count(Action.TOKEN_ISSUE) == 2
    assert kinds.count(Action.TOKEN_REVOKE) == 1
    principal = rig.who_is(new, "10.200.0.2")
    assert isinstance(principal, Authenticated)
    assert principal.principal.id == f"contractor:{PROJECT}"  # the same principal, rotated


def test_the_tokens_of_an_instance_are_all_gone_when_it_stops(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    rig.manager.stop(PROJECT, CONTRACTOR)
    refused = rig.who_is(secret, "10.200.0.2")
    assert isinstance(refused, Rejected)  # revoked by the call itself, before any job ran
    assert refused.reason is Reason.REVOKED
    assert rig.docker.containers[turn_name(PROJECT, CONTRACTOR)]["State"]["Running"]
    rig.run_jobs()


# ----------------------------------------------------------------------------- stop
def test_a_stop_takes_everything_down_but_the_volumes(rig: Rig) -> None:
    rig.start()
    info = rig.manager.stop(PROJECT, CONTRACTOR)
    assert info.state is State.STOPPING
    rig.run_jobs()
    after = rig.manager.status(PROJECT, CONTRACTOR)
    assert (after.state, after.last_turn) == (State.ABSENT, "stopped")
    assert rig.nothing_left()
    assert set(rig.docker.volumes) == {volume_name(PROJECT, CONTRACTOR, v) for v in Volume}
    assert rig.docker.containers["zephyros-hub"]["NetworkSettings"]["Networks"] == {}
    assert (Action.TURN_END, Outcome.OK) in rig.actions()
    assert (Action.TOKEN_REVOKE, Outcome.OK) in rig.actions()


def test_a_stop_of_what_is_not_there_is_harmless(rig: Rig) -> None:
    info = rig.manager.stop(PROJECT, CONTRACTOR)
    assert info.state is State.ABSENT
    assert not rig.manager.step()
    rig.start()
    rig.manager.stop(PROJECT, CONTRACTOR)
    rig.manager.stop(PROJECT, CONTRACTOR)
    rig.run_jobs()
    assert rig.nothing_left()


def test_a_stop_before_the_worker_starts_means_the_start_never_happens(rig: Rig) -> None:
    rig.manager.start(PROJECT, CONTRACTOR, "x")
    rig.manager.stop(PROJECT, CONTRACTOR)
    rig.run_jobs()
    assert rig.nothing_left()
    assert not rig.docker.volumes


@pytest.mark.parametrize(
    ("after", "never_after"),
    [
        ("create_network", "connect_hub"),
        ("connect_hub", "create_volume"),
        ("create_volume", "start_guard"),
        ("start_guard", "start_turn"),
    ],
)
def test_a_stop_in_the_middle_of_a_start_goes_no_further_and_leaves_nothing_behind(
    rig: Rig, after: str, never_after: str
) -> None:
    rig.broker.hooks[after] = lambda: rig.manager.stop(PROJECT, CONTRACTOR)
    rig.start()
    assert never_after not in rig.broker.calls[rig.broker.calls.index(after) :]
    assert rig.nothing_left()
    assert rig.state() is State.ABSENT
    assert (Action.TURN_START, Outcome.OK) not in rig.actions()


def test_a_stop_while_the_guard_is_awaited_ends_the_wait_at_once(rig: Rig) -> None:
    rig.docker.hold_health.add(guard_name(PROJECT, CONTRACTOR))
    rig.broker.hooks["inspect"] = lambda: rig.manager.stop(PROJECT, CONTRACTOR)
    before = rig.clock.now()
    rig.start()
    assert rig.clock.now() - before < timedelta(seconds=5)  # not the thirty of the timeout
    assert rig.nothing_left()
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "stopped"


def test_a_stop_while_the_turn_starts_is_logged_as_a_start_and_an_end(rig: Rig) -> None:
    rig.broker.hooks["start_turn"] = lambda: rig.manager.stop(PROJECT, CONTRACTOR)
    rig.manager.start(PROJECT, CONTRACTOR, "x")
    assert rig.manager.step()  # the start job, with the stop arriving inside it
    assert rig.state() is State.STOPPING  # not overwritten by the end of the start
    assert rig.manager.step()  # the stop job
    assert rig.nothing_left()
    assert rig.state() is State.ABSENT
    kinds = rig.actions()
    assert (Action.TURN_START, Outcome.OK) in kinds  # the container did start
    assert (Action.TURN_END, Outcome.OK) in kinds


def test_a_filter_that_is_down_during_a_stop_is_waited_for(rig: Rig) -> None:
    rig.start()
    rig.manager.stop(PROJECT, CONTRACTOR)
    rig.broker.down = True
    rig.run_jobs()
    assert rig.state() is State.STOPPING
    rig.manager.tick()
    assert rig.state() is State.STOPPING
    rig.broker.down = False
    rig.manager.tick()
    assert rig.state() is State.ABSENT
    assert rig.nothing_left()


# ----------------------------------------------------------------------------- the guard
def test_an_instance_whose_guard_dies_is_taken_down(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    rig.docker.kill(guard_name(PROJECT, CONTRACTOR))
    rig.manager.tick()
    info = rig.manager.status(PROJECT, CONTRACTOR)
    assert (info.state, info.last_turn) == (State.ABSENT, "guard_lost")
    assert rig.nothing_left()
    refused = rig.who_is(secret, "10.200.0.2")
    assert isinstance(refused, Rejected)
    assert refused.reason is Reason.REVOKED
    assert (Action.TURN_END, Outcome.ERROR) in rig.actions()


def test_an_idle_instance_whose_guard_goes_unhealthy_is_taken_down(rig: Rig) -> None:
    rig.start()
    rig.docker.finish(turn_name(PROJECT, CONTRACTOR), 0)
    rig.manager.tick()
    rig.docker.kill_health(guard_name(PROJECT, CONTRACTOR), "unhealthy")
    rig.manager.tick()
    assert rig.state() is State.ABSENT
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "guard_lost"


def test_a_guard_still_starting_is_not_taken_for_lost(rig: Rig) -> None:
    rig.start()
    rig.docker.kill_health(guard_name(PROJECT, CONTRACTOR), "starting")
    rig.manager.tick()
    assert rig.state() is State.TURN


# ------------------------------------------------------------------- a filter that is gone
def test_an_instance_the_hub_cannot_see_is_cut_off_after_a_few_rounds(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    rig.broker.down = True
    for _ in range(LOST_ROUNDS - 1):
        rig.manager.tick()
    assert isinstance(rig.who_is(secret, "10.200.0.2"), Authenticated)
    rig.manager.tick()
    refused = rig.who_is(secret, "10.200.0.2")
    assert isinstance(refused, Rejected)
    assert refused.reason is Reason.REVOKED
    assert rig.state() is State.STOPPING
    rig.broker.down = False
    rig.manager.tick()
    assert rig.state() is State.ABSENT
    assert rig.manager.status(PROJECT, CONTRACTOR).last_turn == "supervision_lost"
    assert rig.nothing_left()


def test_a_filter_that_comes_back_in_time_resets_the_count(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    for _ in range(3):
        rig.broker.down = True
        for _ in range(LOST_ROUNDS - 1):
            rig.manager.tick()
        rig.broker.down = False
        rig.manager.tick()
    assert isinstance(rig.who_is(secret, "10.200.0.2"), Authenticated)
    assert rig.state() is State.TURN


# ----------------------------------------------------------------------------- hub restart
def test_after_a_restart_a_running_turn_is_stopped_and_its_token_revoked(rig: Rig) -> None:
    rig.start()
    secret = rig.turn_secret()
    restarted = rig.make_manager()
    rig.manager = restarted
    restarted.reconcile()
    info = restarted.status(PROJECT, CONTRACTOR)
    assert (info.state, info.last_turn) == (State.IDLE, "hub_restart")
    assert turn_name(PROJECT, CONTRACTOR) not in rig.docker.containers
    assert guard_name(PROJECT, CONTRACTOR) in rig.docker.containers
    refused = rig.who_is(secret, "10.200.0.2")
    assert isinstance(refused, Rejected)
    assert refused.reason is Reason.REVOKED
    rig.start(prompt="again")
    again = rig.turn_secret()
    assert again != secret
    assert isinstance(rig.who_is(again, "10.200.0.2"), Authenticated)
    assert rig.manager.status(PROJECT, CONTRACTOR).address == info.address


def test_after_a_restart_a_new_hub_container_is_attached_again(rig: Rig) -> None:
    rig.start()
    rig.docker.finish(turn_name(PROJECT, CONTRACTOR), 0)
    rig.manager.tick()
    hub = rig.docker.containers["zephyros-hub"]
    hub["NetworkSettings"]["Networks"] = {}  # the hub came back as a new container
    restarted = rig.make_manager()
    rig.manager = restarted
    restarted.reconcile()
    network = hub["NetworkSettings"]["Networks"][network_name(PROJECT, CONTRACTOR)]
    assert network["IPAddress"] == "10.200.0.3"
    rig.start(prompt="again")
    assert rig.state() is State.TURN


def test_after_a_restart_an_instance_the_hub_cannot_rejoin_is_taken_down(rig: Rig) -> None:
    rig.start()
    restarted = rig.make_manager()
    rig.manager = restarted
    hub = rig.docker.containers.pop("zephyros-hub")
    rig.docker.add_hub(managed=False)  # a container that is not marked as the hub
    restarted.reconcile()
    rig.docker.containers["zephyros-hub"] = hub
    rig.run_jobs()
    assert restarted.status(PROJECT, CONTRACTOR).state is State.ABSENT
    assert rig.nothing_left()


def test_after_a_restart_an_instance_without_a_live_guard_is_taken_down(rig: Rig) -> None:
    rig.start()
    rig.docker.kill(guard_name(PROJECT, CONTRACTOR))
    restarted = rig.make_manager()
    rig.manager = restarted
    restarted.reconcile()
    rig.run_jobs()
    assert rig.nothing_left()
    assert restarted.status(PROJECT, CONTRACTOR).state is State.ABSENT


def test_after_a_restart_containers_without_a_record_are_taken_down(rig: Rig) -> None:
    rig.start()
    rig.store.remove(PROJECT, CONTRACTOR)  # a crash between the creation and the record
    restarted = rig.make_manager()
    rig.manager = restarted
    restarted.reconcile()
    rig.run_jobs()
    assert rig.nothing_left()
    assert not [c for c in rig.docker.containers if c.startswith("zs-")]


def test_a_restart_with_no_filter_waits_for_it(rig: Rig) -> None:
    rig.start()
    restarted = rig.make_manager()
    rig.manager = restarted
    rig.broker.down = True
    restarted.tick()
    assert restarted.status(PROJECT, CONTRACTOR).state is State.ABSENT  # unknown, not claimed
    rig.broker.down = False
    restarted.tick()
    assert restarted.status(PROJECT, CONTRACTOR).state is State.IDLE


def test_a_clean_shutdown_ends_the_turns_but_keeps_the_guards(rig: Rig) -> None:
    rig.start(PROJECT, CONTRACTOR)
    rig.start(PROJECT, OWNER)
    secret = rig.turn_secret(PROJECT, OWNER)
    rig.manager.shutdown()
    assert turn_name(PROJECT, OWNER) not in rig.docker.containers
    assert turn_name(PROJECT, CONTRACTOR) not in rig.docker.containers
    assert guard_name(PROJECT, OWNER) in rig.docker.containers
    refused = rig.who_is(secret, str(rig.manager.status(PROJECT, OWNER).address))
    assert isinstance(refused, Rejected)
    assert rig.manager.status(PROJECT, OWNER).last_turn == "hub_stopped"


# ----------------------------------------------------------------------------- the worker
def test_the_worker_does_one_job_at_a_time_and_reports_when_there_is_none(rig: Rig) -> None:
    assert not rig.manager.step()
    rig.manager.start(PROJECT, OWNER, "a")
    rig.manager.start(PROJECT, CONTRACTOR, "b")
    assert rig.manager.step()
    assert rig.state(PROJECT, OWNER) is State.TURN
    assert rig.state(PROJECT, CONTRACTOR) is State.STARTING
    assert rig.manager.step()
    assert not rig.manager.step()


def test_waiting_returns_at_once_when_a_job_is_queued(rig: Rig) -> None:
    rig.manager.start(PROJECT, OWNER, "a")
    rig.manager.wait(30)  # would block for 30 s if it did not see the job
    rig.run_jobs()
    rig.manager.wait(0.01)


# ----------------------------------------------------------------------------- US-E7-01
def test_the_continuity_ladder_wakes_restarts_then_reports(rig: Rig) -> None:
    """US-E7-01 (EXG-21, EXG-22, EXG-45, EXG-59): the task state the hub keeps wakes the
    instance, then restarts it, then the main agent and the human are alerted."""
    seen: list[tuple[str, dict[str, Any]]] = []
    continuity = Continuity(
        ContinuityTiming(inactivity_seconds=900, restarts=1, unblock_seconds=600)
    )

    def record(event: str, payload: Mapping[str, object]) -> NotifyResult:
        seen.append((event, dict(payload)))
        return NotifyResult()

    manager = rig.make_manager(continuity=continuity, notify=record)
    manager.reconcile()
    rig.manager = manager
    rig.start(prompt="First turn.")
    assert rig.state() is State.TURN
    continuity.expect(
        TaskState(PROJECT, "contractor", "delivery", "the base is ready", rig.clock.now())
    )

    # the wake: a turn that states the task and what the hub keeps of it
    rig.clock.advance(900)
    manager.tick()
    rig.run_jobs()
    turn = rig.docker.containers[turn_name(PROJECT, CONTRACTOR)]
    prompt = str(turn["Config"]["Cmd"][1])
    assert "wakes you" in prompt
    assert "the base is ready" in prompt
    assert (Action.CONTINUITY, Outcome.OK) in rig.actions()

    # the restart: the instance goes down, a fresh one takes its place
    before = dict(turn)
    rig.clock.advance(900)
    manager.tick()
    rig.run_jobs()
    restarted = rig.docker.containers[turn_name(PROJECT, CONTRACTOR)]
    assert "restarts your instance" in str(restarted["Config"]["Cmd"][1])
    assert str(restarted["Config"]["Cmd"][1]) != str(before["Config"]["Cmd"][1])

    # the main agent, then the human, are alerted through the notifier, which sends from its
    # own worker: the watchdog only queues the alert
    rig.clock.advance(900)
    manager.tick()
    rig.run_jobs()
    assert seen == []
    rig.send_notifications()
    kinds = [event for event, _ in seen]
    assert kinds == ["continuity.alert_main"]
    rig.clock.advance(600)
    manager.tick()
    rig.send_notifications()
    kinds = [event for event, _ in seen]
    assert kinds == ["continuity.alert_main", "continuity.alert_human"]
    assert seen[-1][1] == {
        "project_id": PROJECT,
        "side": "contractor",
        "step": "alert_human",
        "task": "delivery",
        "detail": "the base is ready",
    }
    # the ladder is walked out; the records say what happened
    assert [a for a, _ in rig.actions()].count(Action.NOTIFICATION) == 2


def test_a_busy_instance_is_not_woken_and_its_activity_counts(rig: Rig) -> None:
    continuity = Continuity(
        ContinuityTiming(inactivity_seconds=900, restarts=1, unblock_seconds=600)
    )
    manager = rig.make_manager(
        replace(TIMING, turn_seconds=10**6, silence_seconds=10**6),
        continuity=continuity,
    )
    manager.reconcile()
    rig.manager = manager
    rig.start(prompt="Working.")
    continuity.expect(TaskState(PROJECT, "contractor", "delivery", "base", rig.clock.now()))
    rig.clock.advance(900)
    manager.tick()  # the turn is still running: nothing is woken
    rig.run_jobs()
    prompt = str(rig.docker.containers[turn_name(PROJECT, CONTRACTOR)]["Config"]["Cmd"][1])
    assert "wakes you" not in prompt
    # the step that could not be taken was not consumed: the wake is still the one proposed
    (step,) = continuity.due(rig.clock.now() + timedelta(seconds=900))
    assert step.kind == "wake"


def test_an_alert_refused_by_the_egress_policy_is_logged(rig: Rig) -> None:
    """EXG-48: the alert never left; the log carries the refused host and the code."""
    continuity = Continuity(
        ContinuityTiming(inactivity_seconds=900, restarts=1, unblock_seconds=600)
    )
    manager = rig.make_manager(
        continuity=continuity,
        notify=lambda _event, _payload: NotifyResult(refused="elsewhere.example"),
    )
    manager.reconcile()
    rig.manager = manager
    continuity.expect(TaskState(PROJECT, "contractor", "delivery", "base", rig.clock.now()))
    rig.clock.advance(900)
    manager.tick()  # the wake
    rig.run_jobs()
    rig.clock.advance(900)
    manager.tick()  # the restart
    rig.run_jobs()
    rig.clock.advance(900)
    manager.tick()  # the main agent is alerted, and the notifier refuses
    rig.send_notifications()
    records = rig.records()
    refused = [r for r in records if r.action is Action.EGRESS]
    assert refused
    assert refused[-1].outcome is Outcome.REFUSED
    assert refused[-1].reason is Reason.EGRESS
    assert "elsewhere.example" in refused[-1].parameters
    sent = [r for r in records if r.action is Action.NOTIFICATION]
    assert sent
    assert sent[-1].outcome is Outcome.ERROR


# ----------------------------------------------------------------------------- egress (ADR 0024)
def test_an_open_instance_is_warned_about_in_the_log_when_a_turn_starts(rig: Rig) -> None:
    rig.manager = rig.make_manager(replace(TIMING, egress="open"))
    rig.manager.reconcile()
    rig.start()
    warnings = [r for r in rig.records() if r.action is Action.INSTANCE_EGRESS]
    assert len(warnings) == 1
    assert warnings[0].outcome is Outcome.OK
    assert '"level":"open"' in warnings[0].parameters
    assert '"side":"contractor"' in warnings[0].parameters


@pytest.mark.parametrize("level", ["regulated", "sealed"])
def test_a_regulated_or_sealed_instance_starts_without_a_warning(rig: Rig, level: str) -> None:
    rig.manager = rig.make_manager(replace(TIMING, egress=level))
    rig.manager.reconcile()
    rig.start()
    assert not [r for r in rig.records() if r.action is Action.INSTANCE_EGRESS]
