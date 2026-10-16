"""The lifecycle of instances: bring up, run turns, watch, take down (ADR 0016).

An instance is a network guard with a fixed address, plus two volumes. It exists from the first
start of a side of a project until that side is stopped. Each piece of work is a turn: one
agent container in the guard's namespace. The project token lives as long as the instance and
is bound to the guard's address; a new one is issued when a turn starts after the hub lost the
old one (a restart) or when the old one is near its end, and the old one is revoked then.

Starting and stopping are requests. `start` and `stop` change what the hub says about the
instance and queue a job; one worker (`step`) does the jobs, one after the other, and `tick`
is the watchdog. Nothing here waits for a container on the thread of a caller, and nothing
here starts a thread: the adapter that runs the worker decides how often to call `step` and
`tick`. This keeps the manager deterministic under test.

The watchdog exists because Hermes does not stop when the hub stops answering (measured on the
pinned version). It ends a turn that ran past its limit or has not called the hub for too
long, stops an instance whose guard stopped (fail-closed), and, when it cannot reach the
container filter for several rounds, cuts an instance off from the hub by revoking its token.
A restart of the hub starts with `reconcile`, which stops every turn it finds: the token of a
turn is lost with the hub's memory.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.broker import Broker, BrokerError, ContainerInfo
from zephyros.core.clock import Clock, format_time
from zephyros.core.config import DEFAULT_EGRESS, Config
from zephyros.core.containers import Role, VolumeKind, guard_address
from zephyros.core.context import DEFAULT_CONTEXT, AgentContext
from zephyros.core.continuity import ANSWER_TASKS, RESTART, WAKE, Continuity
from zephyros.core.errors import Refusal
from zephyros.core.instances import (
    MAX_PROMPT,
    InstanceInfo,
    InstanceRecord,
    InstanceState,
    InstanceStore,
    NoFreeSubnetError,
    allocate_subnet,
)
from zephyros.core.notifications import Notifier
from zephyros.core.obstacles import Briefing
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import instance_principal
from zephyros.core.tokens import TokenService
from zephyros.core.tools import ToolsError, ToolsProbe

SYSTEM_PRINCIPAL = "hub"
LOST_ROUNDS = 3
"""Rounds of the watchdog without an answer from the filter before an instance is cut off."""

Key = tuple[str, Side]


@dataclass(frozen=True, slots=True)
class Timing:
    """Everything the manager reads from the configuration."""

    guard_health_seconds: int
    poll_seconds: int
    turn_seconds: int
    silence_seconds: int
    token_lifetime_seconds: int
    rotate_before_seconds: int
    pool: str
    prefix: int
    egress: str = DEFAULT_EGRESS
    context: AgentContext = DEFAULT_CONTEXT
    """The context the agents work with, recorded at each start of a turn (ADR 0033)."""

    @classmethod
    def from_config(cls, config: Config) -> Timing:
        return cls(
            guard_health_seconds=config.runtime.guard_health_seconds,
            poll_seconds=config.runtime.poll_seconds,
            turn_seconds=config.limits.turn_seconds,
            silence_seconds=config.limits.silence_seconds,
            token_lifetime_seconds=config.tokens.lifetime_seconds,
            rotate_before_seconds=config.tokens.rotate_before_seconds,
            pool=config.network.instance_pool,
            prefix=config.network.instance_prefix,
            egress=config.network.egress,
            context=config.gateway.context,
        )


@dataclass(frozen=True, slots=True)
class TurnEngine:
    """The agent engine a side runs, as each start of a turn records it (ADR 0029): the name the
    configuration gives and the version the image pins."""

    name: str
    version: str


@dataclass(slots=True)
class _Live:
    project_id: str
    side: Side
    phase: InstanceState
    record: InstanceRecord | None = None
    secret: str | None = field(default=None, repr=False)
    token_id: str | None = None
    turn_started: datetime | None = None
    outcome: str | None = None
    created_here: bool = False
    """The instance was created by the job under way: a failure takes it down again."""
    lost_rounds: int = 0

    @property
    def key(self) -> Key:
        return (self.project_id, self.side)


@dataclass(frozen=True, slots=True)
class _Job:
    kind: str
    key: Key
    prompt: str | None = field(default=None, repr=False)
    new_session: bool = False


class TurnBriefing(Protocol):
    """What the hub tells a turn that starts, after its prompt (ADR 0034)."""

    def brief(self, project_id: str, side: Side) -> Briefing | None:
        """The briefing of a turn of `side` that starts now, or None when there is nothing to
        tell."""
        ...

    def briefed(self, project_id: str, briefing: Briefing) -> None:
        """The turn started with `briefing`."""
        ...


class _CancelledError(Exception):
    """A stop was requested while a start was under way."""


class InstanceManager:
    def __init__(
        self,
        broker: Broker,
        store: InstanceStore,
        tokens: TokenService,
        audit: AuditLog,
        clock: Clock,
        timing: Timing,
        *,
        sleep: Callable[[float], None] = time.sleep,
        new_instance_id: Callable[[], str] | None = None,
        continuity: Continuity | None = None,
        notifier: Notifier | None = None,
        engines: Mapping[Side, TurnEngine] | None = None,
        tools: ToolsProbe | None = None,
        briefing: TurnBriefing | None = None,
    ) -> None:
        self._broker = broker
        self._store = store
        self._tokens = tokens
        self._audit = audit
        self._clock = clock
        self._timing = timing
        self._sleep = sleep
        self._new_instance_id = new_instance_id or (lambda: "i_" + secrets.token_hex(6))
        self._continuity = continuity
        self._notifier = notifier
        self._engines = dict(engines or {})
        self._tools = tools
        self._briefing = briefing
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._live: dict[Key, _Live] = {}
        self._jobs: deque[_Job] = deque()
        self._outcomes: dict[Key, str] = {}
        self._reconciled = False

    # ------------------------------------------------------------------ InstanceControl
    def start(
        self, project_id: str, side: Side, prompt: str, *, new_session: bool = False
    ) -> InstanceInfo:
        key = (project_id, side)
        with self._wake:
            if not self._reconciled:
                raise Refusal(Reason.BUSY, "the hub is still checking its instances")
            live = self._live.get(key)
            if live is not None and live.phase is not InstanceState.IDLE:
                raise Refusal(
                    Reason.BUSY,
                    f"the instance is {live.phase.value}; wait for it or stop it first",
                )
            if live is None:
                live = _Live(project_id, side, InstanceState.STARTING)
                self._live[key] = live
            else:
                live.phase = InstanceState.STARTING
            self._jobs.append(_Job("start", key, prompt, new_session))
            self._wake.notify()
            return self._info_locked(key)

    def stop(self, project_id: str, side: Side) -> InstanceInfo:
        key = (project_id, side)
        with self._wake:
            live = self._live.get(key)
            if live is None:
                return self._info_locked(key)
            if live.phase is not InstanceState.STOPPING:
                live.outcome = "stopped"
            live.phase = InstanceState.STOPPING
            record = live.record
            live.secret = None
            self._jobs.append(_Job("stop", key))
            self._wake.notify()
        if record is not None:
            # Before anything is done to the containers: a turn that will not die can no
            # longer call the hub.
            self._revoke(live, record)
        with self._lock:
            return self._info_locked(key)

    def status(self, project_id: str, side: Side) -> InstanceInfo:
        with self._lock:
            return self._info_locked((project_id, side))

    def _info_locked(self, key: Key) -> InstanceInfo:
        live = self._live.get(key)
        if live is None:
            return InstanceInfo(
                key[0], key[1], InstanceState.ABSENT, last_turn=self._outcomes.get(key)
            )
        address = None if live.record is None else str(self._guard_address(live.record))
        started = None if live.turn_started is None else format_time(live.turn_started)
        return InstanceInfo(
            key[0], key[1], live.phase, address, started, live.outcome or self._outcomes.get(key)
        )

    # ------------------------------------------------------------------ the worker
    def step(self) -> bool:
        """Do the next queued job. False when there was none."""
        with self._lock:
            if not self._jobs:
                return False
            job = self._jobs.popleft()
        if job.kind == "start":
            self._run_start(job)
        elif job.kind == "restart":
            self._run_restart(job)
        else:
            self._run_stop(job.key)
        return True

    def wake(self) -> None:
        """Let a thread that is in `wait` go on."""
        with self._wake:
            self._wake.notify_all()

    def wait(self, timeout: float) -> None:
        """Sleep until a job is queued, or `timeout` passes."""
        with self._wake:
            if not self._jobs:
                self._wake.wait(timeout)

    # ------------------------------------------------------------------ start
    def _run_start(self, job: _Job) -> None:
        live = self._live_of(job.key)
        if live is None or live.phase is not InstanceState.STARTING or job.prompt is None:
            return
        try:
            if live.record is None:
                live.created_here = True
                self._bring_up(live)
            self._begin_turn(live, job.prompt, new_session=job.new_session)
        except _CancelledError:
            return
        except NoFreeSubnetError:
            self._start_failed(live, "no_free_subnet")
        except ToolsError as error:
            # The tools changed into something the hub refuses since it started (ADR 0030): no
            # turn runs with tools that the gate would not take either.
            self._start_failed(live, "tools_refused", problems=list(error.problems[:5]))
        except BrokerError as error:
            self._start_failed(live, error.code)
        except Exception:  # reason: a bug must not stop the worker or leak
            self._start_failed(live, "internal_error")
        else:
            live.created_here = False

    def _bring_up(self, live: _Live) -> None:
        project_id, side = live.key
        subnet = allocate_subnet(
            self._timing.pool, self._timing.prefix, [r.subnet for r in self._store.all()]
        )
        record = InstanceRecord(
            project_id, side, self._new_instance_id(), subnet, self._clock.now()
        )
        self._store.put(record)  # before anything exists: a crash leaves a record to reconcile
        live.record = record
        self._broker.create_network(project_id, side, subnet)
        self._check_not_stopping(live)
        self._broker.connect_hub(project_id, side)
        self._check_not_stopping(live)
        for kind in VolumeKind:
            self._broker.create_volume(project_id, side, kind)
        self._check_not_stopping(live)
        self._broker.start_guard(project_id, side, subnet)
        self._wait_healthy(live)

    def _wait_healthy(self, live: _Live) -> None:
        project_id, side = live.key
        deadline = self._clock.now().timestamp() + self._timing.guard_health_seconds
        while True:
            self._check_not_stopping(live)
            guard = self._broker.inspect(Role.GUARD, project_id, side)
            if guard is not None and guard.state == "running" and guard.health == "healthy":
                return
            if guard is None or guard.state != "running" or guard.health == "unhealthy":
                raise BrokerError("guard_failed", "the network guard did not stay up")
            if self._clock.now().timestamp() >= deadline:
                raise BrokerError("guard_timeout", "the network guard did not become healthy")
            self._sleep(1.0)

    def _begin_turn(self, live: _Live, prompt: str, *, new_session: bool = False) -> None:
        project_id, side = live.key
        record = live.record
        assert record is not None  # noqa: S101  # reason: set by _bring_up or by an earlier start
        self._check_not_stopping(live)
        # Measured before the turn starts: the fingerprint the log keeps is that of the tools the
        # turn finds when it starts; nothing measures them again at its end (ADR 0030).
        tools = None if self._tools is None else self._tools.digest()
        secret = self._token_for_turn(live, record)
        briefing = self._brief(project_id, side, prompt)
        if briefing is not None:
            prompt = f"{prompt}\n\n{briefing.text}"
        self._broker.start_turn(project_id, side, secret, prompt, new_session=new_session)
        if briefing is not None and self._briefing is not None:
            self._briefing.briefed(project_id, briefing)
        with self._lock:
            live.turn_started = self._clock.now()
            if live.phase is InstanceState.STARTING:
                live.phase = InstanceState.TURN
                live.outcome = None
                self._outcomes.pop(live.key, None)
            # else a stop arrived meanwhile: the container did start, and the job of the stop
            # takes it down and logs the end of the turn
        engine = self._engines.get(side)
        self._record(
            project_id,
            Action.TURN_START,
            Outcome.OK,
            side=side.value,
            prompt_chars=len(prompt),
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
            **({} if engine is None else {"engine": engine.name, "engine_version": engine.version}),
            **({} if tools is None else {"tools": tools.fingerprint}),
            session="new" if new_session else "continue",
            **({} if briefing is None else {"obstacles": list(briefing.named)}),
            context_window=self._timing.context.window,
            context_tokens=self._timing.context.tokens,
            compaction_tokens=self._timing.context.compaction,
        )
        if self._timing.egress == "open":
            # An exception the operator wrote down (ADR 0024): said again at every start.
            self._record(
                project_id,
                Action.INSTANCE_EGRESS,
                Outcome.OK,
                side=side.value,
                level="open",
                warning="this instance reaches every public destination",
            )

    def _brief(self, project_id: str, side: Side, prompt: str) -> Briefing | None:
        """What the hub tells the turn after its prompt (ADR 0034): the decisions taken on the
        obstacles of the side, and those that await its answer. A prompt with no room left for
        it goes alone: nothing is cut, and the side is told at a later turn."""
        if self._briefing is None:
            return None
        briefing = self._briefing.brief(project_id, side)
        if briefing is None or len(prompt) + 2 + len(briefing.text) > MAX_PROMPT:
            return None
        return briefing

    def _token_for_turn(self, live: _Live, record: InstanceRecord) -> str:
        """The secret to give the turn. A new token replaces the old when the hub no longer holds
        the old one's secret, or when the old one is near its end."""
        principal = instance_principal(live.side, live.project_id)
        current = self._tokens.live_project_tokens(record.instance_id)
        if (
            live.secret is not None
            and current
            and not self._tokens.needs_rotation(current[0], self._timing.rotate_before_seconds)
        ):
            return live.secret
        issued = self._tokens.issue_project_token(
            principal,
            record.instance_id,
            str(guard_address(ipaddress.IPv4Network(record.subnet))),
            self._timing.token_lifetime_seconds,
        )
        with self._lock:
            live.secret = issued.secret
            live.token_id = issued.token_id
        self._record(
            live.project_id,
            Action.TOKEN_ISSUE,
            Outcome.OK,
            token_id=issued.token_id,
            side=live.side.value,
        )
        for old in current:
            if self._tokens.revoke(old.token_id):
                self._record(
                    live.project_id,
                    Action.TOKEN_REVOKE,
                    Outcome.OK,
                    token_id=old.token_id,
                    side=live.side.value,
                    why="rotation",
                )
        return issued.secret

    def _start_failed(self, live: _Live, code: str, **detail: object) -> None:
        self._record(
            live.project_id,
            Action.TURN_START,
            Outcome.ERROR,
            side=live.side.value,
            error=code,
            **detail,
        )
        outcome = f"start_failed:{code}"
        with self._lock:
            self._outcomes[live.key] = outcome
            live.outcome = outcome
            if not live.created_here:
                if live.phase is InstanceState.STARTING:
                    live.phase = InstanceState.IDLE
                return
            live.phase = InstanceState.STOPPING
        self._take_down(live)

    def _check_not_stopping(self, live: _Live) -> None:
        with self._lock:
            if live.phase is InstanceState.STOPPING:
                raise _CancelledError

    # ------------------------------------------------------------------ stop
    def _run_stop(self, key: Key) -> None:
        live = self._live_of(key)
        if live is not None:
            self._take_down(live)

    def _run_restart(self, job: _Job) -> None:
        """The continuity's restart: the instance goes down, a fresh one takes its place,
        and the turn states the task and what the hub keeps of it."""
        self._run_stop(job.key)
        with self._wake:
            self._live[job.key] = _Live(job.key[0], job.key[1], InstanceState.STARTING)
        self._run_start(job)

    def resume(self, project_id: str, side: Side, prompt: str, *, restart: bool) -> bool:
        """The hub resumes a task that stays without activity (US-E7-01). False when the
        instance is busy or the hub is not ready: the step counts as waiting from `observe`."""
        key = (project_id, side)
        with self._wake:
            if not self._reconciled:
                return False
            live = self._live.get(key)
            if live is not None and live.phase is not InstanceState.IDLE:
                return False
            if restart:
                self._jobs.append(_Job("restart", key, prompt))
            else:
                if live is None:
                    self._live[key] = _Live(project_id, side, InstanceState.STARTING)
                else:
                    live.phase = InstanceState.STARTING
                self._jobs.append(_Job("start", key, prompt))
            self._wake.notify()
        return True

    def _take_down(self, live: _Live) -> None:
        """Revoke, then remove the containers and the network; keep the volumes. If the filter
        does not answer, the instance stays `stopping` and the watchdog tries again."""
        project_id, side = live.key
        record = live.record
        if record is not None:
            self._revoke(live, record)
        try:
            self._broker.stop_turn(project_id, side)
            self._broker.stop_guard(project_id, side)
            self._broker.disconnect_hub(project_id, side)
            self._broker.remove_network(project_id, side)
        except BrokerError:
            return
        was_turn = live.turn_started is not None
        with self._lock:
            live.turn_started = None
            self._live.pop(live.key, None)
            self._outcomes[live.key] = live.outcome or "stopped"
        if record is not None:
            self._store.remove(project_id, side)
        if was_turn and live.outcome == "stopped":
            self._record(project_id, Action.TURN_END, Outcome.OK, side=side.value, result="stopped")

    def _revoke(self, live: _Live, record: InstanceRecord) -> None:
        count = self._tokens.revoke_instance(record.instance_id)
        with self._lock:
            live.secret = None
        if count:
            self._record(
                live.project_id,
                Action.TOKEN_REVOKE,
                Outcome.OK,
                side=live.side.value,
                count=count,
                why="stop",
            )

    # ------------------------------------------------------------------ the watchdog
    def tick(self) -> None:
        """One round of the watchdog. Safe to call on every poll."""
        if not self._reconciled:
            try:
                self.reconcile()
            except BrokerError:
                return
        with self._lock:
            lives = list(self._live.values())
        for live in lives:
            try:
                self._watch(live)
            except BrokerError:
                self._not_answered(live)
            else:
                live.lost_rounds = 0
        self._continuity_tick()

    def _continuity_tick(self) -> None:
        """Walk the escalation ladder of the tasks that stay without activity (US-E7-01)."""
        if self._continuity is None:
            return
        for step in self._continuity.due(self._clock.now()):
            project_id = step.expectation.project_id
            # the side that carries the task: an instance's, or the main agent for a decision
            # it is to take (ADR 0023), whose ladder wakes no instance
            side = step.expectation.side
            state = {
                "project_id": project_id,
                "side": side,
                "step": step.kind,
                "task": step.expectation.kind,
                "detail": step.expectation.detail,
            }
            record_state = {key: str(value) for key, value in state.items() if key != "project_id"}
            if step.kind in (WAKE, RESTART):
                restart = step.kind == RESTART
                if not self.resume(project_id, Side(side), step.prompt, restart=restart):
                    # The instance is busy. An active instance is not without activity, save on
                    # an obstacle, which only an answer counts on: its step stays due, and is
                    # taken once the turn ends (ADR 0034).
                    if step.expectation.kind not in ANSWER_TASKS:
                        self._continuity.observe(project_id, side, self._clock.now())
                    continue
            elif self._notifier is not None:
                # queued: the watchdog never waits for the receiver
                self._notifier.notify(
                    "continuity." + step.kind, state, stream=project_id, record=record_state
                )
            self._continuity.taken(step, self._clock.now())
            self._record(project_id, Action.CONTINUITY, Outcome.OK, **record_state)

    def _watch(self, live: _Live) -> None:
        project_id, side = live.key
        if live.phase is InstanceState.STOPPING:
            self._take_down(live)
            return
        if live.phase is InstanceState.STARTING:
            return  # the worker is on it
        guard = self._broker.inspect(Role.GUARD, project_id, side)
        if guard is None or guard.state != "running" or guard.health == "unhealthy":
            self._guard_lost(live)
            return
        if not self._level_holds(project_id, side, guard):
            self._guard_lost(live, "egress_changed")
            return
        if live.phase is not InstanceState.TURN:
            return
        turn = self._broker.inspect(Role.TURN, project_id, side)
        if turn is None or turn.state in ("exited", "dead"):
            self._broker.stop_turn(project_id, side)
            self._end_turn(live, _result_of(turn))
            return
        started = live.turn_started
        assert started is not None  # noqa: S101  # reason: a turn has a start
        now = self._clock.now()
        if (now - started).total_seconds() > self._timing.turn_seconds:
            self._broker.stop_turn(project_id, side)
            self._end_turn(live, "timeout")
            return
        heard = started
        if live.token_id is not None:
            seen = self._tokens.last_seen(live.token_id)
            if seen is not None and seen > heard:
                heard = seen
        if (now - heard).total_seconds() > self._timing.silence_seconds:
            self._broker.stop_turn(project_id, side)
            self._end_turn(live, "silent")

    def _end_turn(self, live: _Live, result: str) -> None:
        with self._lock:
            if live.phase is not InstanceState.TURN:
                return
            live.phase = InstanceState.IDLE
            live.turn_started = None
            live.outcome = result
            self._outcomes[live.key] = result
        ok = result in ("completed", "stopped")
        self._record(
            live.project_id,
            Action.TURN_END,
            Outcome.OK if ok else Outcome.ERROR,
            side=live.side.value,
            result=result,
        )

    def _guard_lost(self, live: _Live, result: str = "guard_lost") -> None:
        """Fail-closed: a guard that is not there, or that holds the rules of another level,
        means the instance is not either."""
        with self._lock:
            live.phase = InstanceState.STOPPING
            live.outcome = result
        self._record(
            live.project_id,
            Action.TURN_END,
            Outcome.ERROR,
            side=live.side.value,
            result=result,
        )
        self._take_down(live)

    def _not_answered(self, live: _Live) -> None:
        live.lost_rounds += 1
        if live.lost_rounds < LOST_ROUNDS or live.phase is InstanceState.STOPPING:
            return
        # The hub can no longer see the instance. It cannot stop it either, but it can stop
        # answering it: no token, no model, no tools.
        with self._lock:
            live.phase = InstanceState.STOPPING
            live.outcome = "supervision_lost"
        self._record(
            live.project_id,
            Action.TURN_END,
            Outcome.ERROR,
            side=live.side.value,
            result="supervision_lost",
        )
        if live.record is not None:
            self._revoke(live, live.record)

    # ------------------------------------------------------------------ restart and shutdown
    def reconcile(self) -> None:
        """Take stock after a start of the hub. Raises BrokerError when the filter does not
        answer, and may be called again."""
        containers = self._broker.list_containers()
        found: dict[Key, dict[str, ContainerInfo]] = {}
        for info in containers:
            found.setdefault((info.project_id, Side(info.side)), {})[info.role] = info
        records = {(r.project_id, r.side): r for r in self._store.all()}
        for key, record in records.items():
            here = found.pop(key, {})
            live = _Live(key[0], key[1], InstanceState.IDLE, record=record)
            self._tokens.revoke_instance(record.instance_id)  # the secret died with the hub
            if "turn" in here:
                self._broker.stop_turn(*key)
                live.outcome = "hub_restart"
                self._outcomes[key] = "hub_restart"
                self._record(
                    key[0],
                    Action.TURN_END,
                    Outcome.ERROR,
                    side=key[1].value,
                    result="hub_restart",
                )
            guard = here.get("guard")
            if (
                guard is None
                or guard.state != "running"
                or guard.health == "unhealthy"
                or not self._level_holds(key[0], key[1], guard)
            ):
                live.phase = InstanceState.STOPPING
            else:
                # The hub may be a new container since the last run: attach it again, at the
                # same fixed address, which the guard's rules name.
                try:
                    self._broker.connect_hub(*key)
                except BrokerError:
                    live.phase = InstanceState.STOPPING
            with self._lock:
                self._live[key] = live
                if live.phase is InstanceState.STOPPING:
                    self._jobs.append(_Job("stop", key))
        for key in found:  # containers with no record: leftovers of a crash
            orphan = _Live(key[0], key[1], InstanceState.STOPPING)
            with self._lock:
                self._live[key] = orphan
                self._jobs.append(_Job("stop", key))
        with self._wake:
            self._reconciled = True
            self._wake.notify()

    def shutdown(self) -> None:
        """The hub is stopping: end the turns that run, so none outlives it. Guards stay."""
        with self._lock:
            running = [live for live in self._live.values() if live.phase is InstanceState.TURN]
        for live in running:
            if live.record is not None:
                self._revoke(live, live.record)
            try:
                self._broker.stop_turn(*live.key)
            except BrokerError:
                continue
            self._end_turn(live, "hub_stopped")

    # ------------------------------------------------------------------ helpers
    def _live_of(self, key: Key) -> _Live | None:
        with self._lock:
            return self._live.get(key)

    def _level_holds(self, project_id: str, side: Side, guard: ContainerInfo) -> bool:
        """Whether a guard holds the rules of the configured level (ADR 0024). Its rules are
        loaded once, when it starts: one of another level (made before a change of the
        configuration, or before the levels existed and so with no label) is logged and must
        be taken down, so that the next start brings it up at the configured level."""
        if guard.egress == self._timing.egress:
            return True
        self._record(
            project_id,
            Action.INSTANCE_EGRESS,
            Outcome.ERROR,
            side=side.value,
            why="level_changed",
            guard=guard.egress or "none",
            configured=self._timing.egress,
        )
        return False

    @staticmethod
    def _guard_address(record: InstanceRecord) -> ipaddress.IPv4Address:
        return guard_address(ipaddress.IPv4Network(record.subnet))

    def _record(
        self,
        project_id: str,
        action: Action,
        outcome: Outcome,
        *,
        reason: object = Reason.NONE,
        **parameters: object,
    ) -> None:
        self._audit.append(
            AuditEvent(
                stream=project_id,
                principal=SYSTEM_PRINCIPAL,
                action=action,
                outcome=outcome,
                reason=reason if isinstance(reason, Reason) else Reason(str(reason)),
                parameters=parameters,
            )
        )


def _result_of(turn: ContainerInfo | None) -> str:
    if turn is None:
        return "failed:vanished"
    if turn.exit_code == 0:
        return "completed"
    return f"failed:exit_{turn.exit_code}"
