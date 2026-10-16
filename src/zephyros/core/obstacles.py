"""Obstacles: how an agent that is blocked says so, and who answers (ADR 0034).

An agent that meets something it cannot change — the environment of its instance, a tool of the
project, the contract, an acceptance test, its budget — reports an obstacle: a bounded piece of
evidence, not a conversation. Each obstacle awaits an answer from one level of a chain, and goes
up it only when someone decides so:

- an obstacle of the Contractor awaits the Owner side, which clarifies the need, amends the
  contract, fixes an acceptance test, rejects it with its reasons, or escalates it;
- an obstacle of the Owner side, or one the Owner side escalated, awaits the operator, and the
  main agent within its rights: they answer it, handle it (a new turn, a new session, a stop), or
  escalate it to the human;
- an obstacle escalated to the human awaits the operator channel, the human's own.

Its life: open, then answered, rejected or escalated (escalated awaits the next level), then
closed by its author or the operator. Each step is a call of the hub, so the log keeps it.

Who reads what follows who it concerns: the Contractor reads its own obstacles only; the Owner
side reads its own and the Contractor's; the operator and the main agent read every one. The
Contractor reads every answer to its obstacles, so an answer that copies a long passage of an
acceptance test the hub keeps for the Owner side is refused (`copied_passage`).

Nothing here reaches anything but the hub's own records: an obstacle names evidence, it never
reads it, and it changes no deadline.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from zephyros.core.audit import Reason
from zephyros.core.clock import Clock, format_time, parse_time
from zephyros.core.errors import Refusal
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import Principal, PrincipalKind
from zephyros.core.tools import TOOLS_PATH

NATURES = ("environment", "tool", "contract", "acceptance_test", "budget", "other")
"""What stands in the way: the instance, a tool of the project or of the hub, the contract, an
acceptance test, a limit of time or calls, or something else."""
EVIDENCE_KINDS = ("path", "command", "delivery", "log")
HANDLED = ("relaunched", "new_session", "stopped", "environment_fixed", "other")
"""What the operator or the main agent did about an obstacle it handled."""

MAX_AWAITING = 5
"""Obstacles of one side of a project that may await an answer at once."""
MAX_PER_TURN = 3
"""Obstacles one side may report in one turn."""
TURN_WINDOW = timedelta(hours=1)
"""Where the hub does not know when the turn of the reporting side began: the window counted."""

TITLE_CHARS = 120
DESCRIPTION_CHARS = 4000
TRIED_CHARS = 2000
NEEDED_CHARS = 1000
EVIDENCE_ITEMS = 10
COMMAND_CHARS = 500
OUTPUT_CHARS = 2000
NOTE_CHARS = 300
PATH_CHARS = 300
ANSWER_CHARS = 4000
CLOSE_NOTE_CHARS = 1000
FIXED_PATHS = 10
LIST_DEFAULT = 50
LIST_LIMIT = 200
BRIEFED = 5
"""The most obstacles a briefing names in each of its lists; it counts the others."""

COPY_WINDOW = 120
COPY_STRIDE = 40
COPIED_CHARS = COPY_WINDOW + COPY_STRIDE
"""A passage of an acceptance test this long, whitespace aside, is always found in an answer;
a shorter one may be, from COPY_WINDOW characters."""

OBSTACLE_ID = r"OB-[0-9]{3,6}"
_OBSTACLE_ID = re.compile(OBSTACLE_ID)
_WHITESPACE = re.compile(r"\s+")


class Status(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    """Handed to the next level, which it now awaits."""
    CLOSED = "closed"


AWAITING = frozenset({Status.OPEN, Status.ESCALATED})
"""The states in which an obstacle awaits an answer from its level."""


class Level(StrEnum):
    """Who is to answer an obstacle now."""

    OWNER = "owner"
    OPERATOR = "operator"
    """The operator, and the main agent."""
    HUMAN = "human"
    """The human, through the operator channel."""


class Decision(StrEnum):
    CLARIFY = "clarify"
    """The need, made precise."""
    AMEND = "amend"
    """An amendment of the contract: a change proposed or applied (`change_id`)."""
    FIX_TEST = "fix_test"
    """Acceptance tests corrected (`paths`)."""
    HANDLE = "handle"
    """The operator or the main agent acted (`action`)."""
    REJECT = "reject"
    """Not an obstacle of the environment or the contract: the reasons say why (a fault of the
    delivery, a question of how to build)."""
    ESCALATE = "escalate"
    CLOSE = "close"


ANSWERS: Mapping[Level, tuple[Decision, ...]] = {
    Level.OWNER: (
        Decision.CLARIFY,
        Decision.AMEND,
        Decision.FIX_TEST,
        Decision.REJECT,
        Decision.ESCALATE,
    ),
    Level.OPERATOR: (
        Decision.CLARIFY,
        Decision.AMEND,
        Decision.FIX_TEST,
        Decision.HANDLE,
        Decision.REJECT,
        Decision.ESCALATE,
    ),
    Level.HUMAN: (
        Decision.CLARIFY,
        Decision.AMEND,
        Decision.FIX_TEST,
        Decision.HANDLE,
        Decision.REJECT,
    ),
}
"""The decisions each level may take."""
NEXT_LEVEL: Mapping[Level, Level] = {Level.OWNER: Level.OPERATOR, Level.OPERATOR: Level.HUMAN}
ANSWERERS: Mapping[Level, frozenset[PrincipalKind]] = {
    Level.OWNER: frozenset({PrincipalKind.OWNER, PrincipalKind.OPERATOR}),
    Level.OPERATOR: frozenset({PrincipalKind.OPERATOR, PrincipalKind.MAIN_AGENT}),
    Level.HUMAN: frozenset({PrincipalKind.OPERATOR}),
}
"""Who may answer an obstacle at each level. The operator channel is the human's own, so it
answers at every level; the main agent answers what reached the operator."""
_STATUS_OF = {
    Decision.CLARIFY: Status.ANSWERED,
    Decision.AMEND: Status.ANSWERED,
    Decision.FIX_TEST: Status.ANSWERED,
    Decision.HANDLE: Status.ANSWERED,
    Decision.REJECT: Status.REJECTED,
    Decision.ESCALATE: Status.ESCALATED,
}


@dataclass(frozen=True, slots=True)
class Entry:
    """One decision taken on an obstacle."""

    decision: Decision
    by: str
    at: datetime
    level: Level
    """The level the obstacle stood at when the decision was taken."""
    text: str = ""
    change_id: str | None = None
    paths: tuple[str, ...] = ()
    action: str | None = None


@dataclass(frozen=True, slots=True)
class Obstacle:
    project_id: str
    number: int
    author: str
    side: Side
    nature: str
    blocking: bool
    title: str
    description: str
    evidence: tuple[Mapping[str, object], ...]
    tried: str
    needed: str
    reported_at: datetime
    status: Status
    level: Level
    entries: tuple[Entry, ...] = ()
    seen: int = 0
    """How many of the entries its author has been told of: read, or named in a briefing."""

    @property
    def id(self) -> str:
        return obstacle_id(self.number)

    @property
    def awaiting(self) -> bool:
        return self.status in AWAITING

    @property
    def updated_at(self) -> datetime:
        return self.entries[-1].at if self.entries else self.reported_at

    @property
    def unseen(self) -> bool:
        """Whether a decision of someone else awaits the eyes of its author."""
        return any(entry.by != self.author for entry in self.entries[self.seen :])


def obstacle_id(number: int) -> str:
    return f"OB-{number:03d}"


def parse_obstacle_id(text: str) -> int:
    if not _OBSTACLE_ID.fullmatch(text):
        raise Refusal(Reason.INVALID_REQUEST, f"{text!r} is not an obstacle identifier")
    return int(text[3:])


class ObstacleStore(Protocol):
    """Where the obstacles are kept, so a restart of the hub finds them."""

    def add(self, obstacle: Obstacle) -> None:
        """Keep a new obstacle. Its number follows the last of its project."""
        ...

    def put(self, obstacle: Obstacle) -> None:
        """Replace the obstacle of the same project and number."""
        ...

    def get(self, project_id: str, number: int) -> Obstacle | None: ...

    def of_project(self, project_id: str) -> list[Obstacle]:
        """Every obstacle of the project, by number."""
        ...


# ----------------------------------------------------------------------------- the rights
def may_read(principal: Principal, obstacle: Obstacle) -> bool:
    """Whether the obstacle concerns `principal`: the operator and the main agent read every
    one; the Owner side those of its project; the Contractor its own only, never those of the
    Owner side, which may speak of the acceptance tests it keeps hidden."""
    if principal.kind in (PrincipalKind.OPERATOR, PrincipalKind.MAIN_AGENT):
        return True
    if principal.project_id != obstacle.project_id:
        return False
    if principal.kind is PrincipalKind.OWNER:
        return True
    return obstacle.side is Side.CONTRACTOR


def check_answer(principal: Principal, obstacle: Obstacle, decision: Decision) -> None:
    """Refuse an answer that `principal` may not give to `obstacle` now."""
    if not obstacle.awaiting:
        raise Refusal(
            Reason.NOT_PENDING, f"{obstacle.id} is {obstacle.status.value}: it awaits no answer"
        )
    elsewhere = principal.is_instance and principal.project_id != obstacle.project_id
    if (
        elsewhere
        or principal.kind not in ANSWERERS[obstacle.level]
        or principal.id == obstacle.author
    ):
        raise Refusal(
            Reason.FORBIDDEN,
            f"{obstacle.id} awaits an answer of the {_level_name(obstacle.level)}",
        )
    if decision not in ANSWERS[obstacle.level]:
        raise Refusal(
            Reason.INVALID_REQUEST,
            f"the {_level_name(obstacle.level)} answers with one of: "
            + ", ".join(d.value for d in ANSWERS[obstacle.level]),
        )


def check_close(principal: Principal, obstacle: Obstacle) -> None:
    if obstacle.status is Status.CLOSED:
        raise Refusal(Reason.NOT_PENDING, f"{obstacle.id} is closed already")
    if principal.id != obstacle.author and principal.kind is not PrincipalKind.OPERATOR:
        raise Refusal(Reason.FORBIDDEN, "its author or the operator closes an obstacle")


def _level_name(level: Level) -> str:
    return {
        Level.OWNER: "Owner side",
        Level.OPERATOR: "operator or the main agent",
        Level.HUMAN: "human, through the operator channel",
    }[level]


def awaited_from(side: Side) -> Level:
    """Who answers an obstacle reported by `side` first."""
    return Level.OWNER if side is Side.CONTRACTOR else Level.OPERATOR


# ----------------------------------------------------------------------------- the evidence
def parse_evidence(items: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """The evidence of a report, each item with the fields of its kind and nothing else. The
    hub's input schema has checked each field; this checks which fields go with which kind."""
    fields = {
        "path": ({"path"}, {"note"}),
        "command": ({"command"}, {"output", "exit_code", "note"}),
        "delivery": ({"delivery_id"}, {"note"}),
        "log": ({"seq"}, {"note"}),
    }
    parsed: list[dict[str, object]] = []
    for index, item in enumerate(items):
        where = f"evidence[{index}]"
        kind = str(item.get("kind"))
        required, optional = fields[kind]
        given = set(item) - {"kind"}
        missing = sorted(required - given)
        extra = sorted(given - required - optional)
        if missing:
            raise Refusal(Reason.INVALID_REQUEST, f"{where}: a {kind} names its {missing[0]}")
        if extra:
            raise Refusal(Reason.INVALID_REQUEST, f"{where}: {extra[0]} is not a field of a {kind}")
        if kind == "path":
            _check_path(str(item["path"]), where)
        parsed.append({"kind": kind, **{key: item[key] for key in sorted(given)}})
    return tuple(parsed)


def _check_path(path: str, where: str) -> None:
    roots = ("/work", TOOLS_PATH)
    inside = any(path == root or path.startswith(root + "/") for root in roots)
    if not inside or ".." in path.split("/"):
        raise Refusal(
            Reason.INVALID_REQUEST,
            f"{where}: a path is under /work or {TOOLS_PATH}, without '..'",
        )


# ----------------------------------------------------------------------------- copies
def _normalised(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def copied_passage(text: str, files: Mapping[str, bytes]) -> str | None:
    """The first acceptance test file, by path, of which `text` copies a passage of
    COPIED_CHARS characters or more, whitespace aside; None when it copies none.

    The windows of the text taken every COPY_STRIDE characters, COPY_WINDOW long, are looked
    for in each file: a copied passage of COPIED_CHARS characters holds one of them whole. A
    passage rephrased, split, re-encoded or given over several answers is not found."""
    answer = _normalised(text)
    if len(answer) < COPY_WINDOW:
        return None
    windows = {
        answer[start : start + COPY_WINDOW]
        for start in range(0, len(answer) - COPY_WINDOW + 1, COPY_STRIDE)
    }
    for path in sorted(files):
        content = _normalised(files[path].decode("utf-8", errors="replace"))
        if len(content) >= COPY_WINDOW and any(window in content for window in windows):
            return path
    return None


# ----------------------------------------------------------------------------- the record
def obstacle_to_data(obstacle: Obstacle) -> dict[str, object]:
    """The obstacle as JSON data: what a store keeps."""
    return {
        "project_id": obstacle.project_id,
        "number": obstacle.number,
        "author": obstacle.author,
        "side": obstacle.side.value,
        "nature": obstacle.nature,
        "blocking": obstacle.blocking,
        "title": obstacle.title,
        "description": obstacle.description,
        "evidence": [dict(item) for item in obstacle.evidence],
        "tried": obstacle.tried,
        "needed": obstacle.needed,
        "reported_at": format_time(obstacle.reported_at),
        "status": obstacle.status.value,
        "level": obstacle.level.value,
        "entries": [_entry_data(entry) for entry in obstacle.entries],
        "seen": obstacle.seen,
    }


def obstacle_from_data(data: Mapping[str, object]) -> Obstacle:
    """The inverse of `obstacle_to_data`."""
    evidence = data["evidence"]
    entries = data["entries"]
    assert isinstance(evidence, list)  # noqa: S101  # reason: written by obstacle_to_data
    assert isinstance(entries, list)  # noqa: S101  # reason: written by obstacle_to_data
    return Obstacle(
        project_id=str(data["project_id"]),
        number=int(str(data["number"])),
        author=str(data["author"]),
        side=Side(str(data["side"])),
        nature=str(data["nature"]),
        blocking=bool(data["blocking"]),
        title=str(data["title"]),
        description=str(data["description"]),
        evidence=tuple(dict(item) for item in evidence),
        tried=str(data["tried"]),
        needed=str(data["needed"]),
        reported_at=parse_time(str(data["reported_at"])),
        status=Status(str(data["status"])),
        level=Level(str(data["level"])),
        entries=tuple(_entry_from_data(item) for item in entries),
        seen=int(str(data["seen"])),
    )


def _entry_data(entry: Entry) -> dict[str, object]:
    data: dict[str, object] = {
        "decision": entry.decision.value,
        "by": entry.by,
        "at": format_time(entry.at),
        "level": entry.level.value,
        "text": entry.text,
    }
    if entry.change_id is not None:
        data["change_id"] = entry.change_id
    if entry.paths:
        data["paths"] = list(entry.paths)
    if entry.action is not None:
        data["action"] = entry.action
    return data


def _entry_from_data(data: Mapping[str, object]) -> Entry:
    paths = data.get("paths") or []
    assert isinstance(paths, list)  # noqa: S101  # reason: written by _entry_data
    change_id = data.get("change_id")
    action = data.get("action")
    return Entry(
        decision=Decision(str(data["decision"])),
        by=str(data["by"]),
        at=parse_time(str(data["at"])),
        level=Level(str(data["level"])),
        text=str(data.get("text", "")),
        change_id=None if change_id is None else str(change_id),
        paths=tuple(str(path) for path in paths),
        action=None if action is None else str(action),
    )


def obstacle_view(obstacle: Obstacle) -> dict[str, object]:
    """The whole obstacle as a reader sees it (`obstacle.read`)."""
    data = obstacle_to_data(obstacle)
    data["obstacle_id"] = obstacle.id
    data["awaits"] = obstacle.level.value if obstacle.awaiting else None
    data["decisions"] = data.pop("entries")
    for key in ("number", "seen"):
        data.pop(key)
    return data


def obstacle_summary(obstacle: Obstacle) -> dict[str, object]:
    """One line of `obstacle.list`."""
    last = obstacle.entries[-1] if obstacle.entries else None
    return {
        "project_id": obstacle.project_id,
        "obstacle_id": obstacle.id,
        "side": obstacle.side.value,
        "nature": obstacle.nature,
        "blocking": obstacle.blocking,
        "title": obstacle.title,
        "status": obstacle.status.value,
        "level": obstacle.level.value,
        "awaits": obstacle.level.value if obstacle.awaiting else None,
        "reported_at": format_time(obstacle.reported_at),
        "updated_at": format_time(obstacle.updated_at),
        "last_decision": None if last is None else last.decision.value,
    }


def task_detail(obstacle: Obstacle) -> str:
    """How the continuity names the task of answering a blocking obstacle: the hub's own words,
    never the agent's."""
    who = "the Contractor" if obstacle.side is Side.CONTRACTOR else "the Owner side"
    return (
        f"obstacle {obstacle.id} ({obstacle.nature}, blocking) of {who} awaits an answer of the "
        f"{_level_name(obstacle.level)}"
    )


# ----------------------------------------------------------------------------- briefing
@dataclass(frozen=True, slots=True)
class Briefing:
    """What the hub tells a side at the start of a turn, after its prompt."""

    text: str
    answered: tuple[str, ...]
    """The obstacles of the side whose decisions the briefing names: once the turn starts, its
    author has been told of them."""
    awaiting: tuple[str, ...]
    """The obstacles that await an answer of the side."""

    @property
    def named(self) -> tuple[str, ...]:
        return self.answered + self.awaiting


def _named(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:BRIEFED])
    return shown + (f" and {len(ids) - BRIEFED} more" if len(ids) > BRIEFED else "")


# ----------------------------------------------------------------------------- the service
class ObstacleService:
    """The obstacles of every project. One lock covers a read and the write that follows it."""

    def __init__(self, store: ObstacleStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()

    def report(
        self,
        principal: Principal,
        project_id: str,
        fields: Mapping[str, object],
        evidence: tuple[dict[str, object], ...],
        *,
        turn_started: datetime | None,
    ) -> Obstacle:
        """Keep the obstacle `principal` reports, within the bounds of its side."""
        side = principal.side
        if side is None or principal.project_id != project_id:
            raise Refusal(Reason.FORBIDDEN, "an instance reports the obstacles of its project")
        now = self._clock.now()
        with self._lock:
            known = self._store.of_project(project_id)
            mine = [item for item in known if item.side is side]
            waiting = sum(1 for item in mine if item.awaiting)
            if waiting >= MAX_AWAITING:
                raise Refusal(
                    Reason.TOO_MANY_OBSTACLES,
                    f"{waiting} obstacles of this side await an answer: at most {MAX_AWAITING}",
                )
            since = turn_started if turn_started is not None else now - TURN_WINDOW
            this_turn = sum(1 for item in mine if item.reported_at >= since)
            if this_turn >= MAX_PER_TURN:
                raise Refusal(
                    Reason.TOO_MANY_OBSTACLES,
                    f"{this_turn} obstacles reported in this turn: at most {MAX_PER_TURN}",
                )
            obstacle = Obstacle(
                project_id=project_id,
                number=max((item.number for item in known), default=0) + 1,
                author=principal.id,
                side=side,
                nature=str(fields["nature"]),
                blocking=bool(fields["blocking"]),
                title=str(fields["title"]),
                description=str(fields["description"]),
                evidence=evidence,
                tried=str(fields["tried"]),
                needed=str(fields["needed"]),
                reported_at=now,
                status=Status.OPEN,
                level=awaited_from(side),
            )
            self._store.add(obstacle)
        return obstacle

    def get(self, principal: Principal, project_id: str, text_id: str) -> Obstacle:
        """The obstacle, if it concerns `principal`."""
        obstacle = self._store.get(project_id, parse_obstacle_id(text_id))
        if obstacle is None:
            raise Refusal(Reason.NOT_FOUND, f"there is no obstacle {text_id} in this project")
        if not may_read(principal, obstacle):
            raise Refusal(Reason.FORBIDDEN, f"{text_id} is not an obstacle of yours to read")
        return obstacle

    def read(self, principal: Principal, project_id: str, text_id: str) -> Obstacle:
        """The obstacle; its author has now seen every decision taken on it."""
        with self._lock:
            obstacle = self.get(principal, project_id, text_id)
            if principal.id == obstacle.author and obstacle.seen < len(obstacle.entries):
                obstacle = replace(obstacle, seen=len(obstacle.entries))
                self._store.put(obstacle)
        return obstacle

    def visible(self, principal: Principal, project_ids: Iterable[str]) -> list[Obstacle]:
        """The obstacles of these projects that concern `principal`, the most recent first."""
        found = [
            obstacle
            for project_id in project_ids
            for obstacle in self._store.of_project(project_id)
            if may_read(principal, obstacle)
        ]
        return sorted(found, key=lambda item: (item.reported_at, item.number), reverse=True)

    def of_project(self, project_id: str) -> list[Obstacle]:
        return self._store.of_project(project_id)

    def answer(
        self,
        principal: Principal,
        project_id: str,
        text_id: str,
        entry: Entry,
        check: Callable[[Obstacle], None] | None = None,
    ) -> tuple[Obstacle, Obstacle]:
        """Take the decision of `entry` on the obstacle. `check` sees the obstacle first, and
        may refuse. Returns the obstacle before and after."""
        with self._lock:
            before = self.get(principal, project_id, text_id)
            check_answer(principal, before, entry.decision)
            if check is not None:
                check(before)
            status = _STATUS_OF[entry.decision]
            level = NEXT_LEVEL[before.level] if status is Status.ESCALATED else before.level
            after = replace(
                before,
                status=status,
                level=level,
                entries=(*before.entries, replace(entry, level=before.level)),
            )
            self._store.put(after)
        return before, after

    def close(
        self,
        principal: Principal,
        project_id: str,
        text_id: str,
        note: str,
        check: Callable[[Obstacle], None] | None = None,
    ) -> Obstacle:
        with self._lock:
            before = self.get(principal, project_id, text_id)
            check_close(principal, before)
            if check is not None:
                check(before)
            entry = Entry(Decision.CLOSE, principal.id, self._clock.now(), before.level, note)
            entries = (*before.entries, entry)
            seen = len(entries) if principal.id == before.author else before.seen
            after = replace(before, status=Status.CLOSED, entries=entries, seen=seen)
            self._store.put(after)
        return after

    def oldest_blocking(self, project_id: str, level: Level) -> Obstacle | None:
        """The oldest blocking obstacle of the project that awaits an answer at `level`."""
        for obstacle in self._store.of_project(project_id):
            if obstacle.blocking and obstacle.awaiting and obstacle.level is level:
                return obstacle
        return None

    # ------------------------------------------------------------------ the turn briefing
    def brief(self, project_id: str, side: Side) -> Briefing | None:
        """What to tell a turn of `side` that starts: the decisions on its obstacles it has not
        been told of, and, for the Owner side, the obstacles that await its answer."""
        obstacles = self._store.of_project(project_id)
        answered = [item.id for item in obstacles if item.side is side and item.unseen]
        awaiting = [
            item
            for item in obstacles
            if side is Side.OWNER and item.awaiting and item.level is Level.OWNER
        ]
        if not answered and not awaiting:
            return None
        parts = ["[Zephyros hub]"]
        if answered:
            parts.append(
                f"Decisions were taken on your obstacles {_named(answered)}: read them with "
                "obstacle.read."
            )
        if awaiting:
            ids = [item.id + (" (blocking)" if item.blocking else "") for item in awaiting]
            parts.append(
                f"Obstacles of the Contractor await your answer: {_named(ids)}; read them with "
                "obstacle.read and answer with obstacle.answer."
            )
        return Briefing(" ".join(parts), tuple(answered), tuple(item.id for item in awaiting))

    def briefed(self, project_id: str, briefing: Briefing) -> None:
        """The turn started with `briefing`: its author has been told of those decisions."""
        with self._lock:
            for text_id in briefing.answered:
                obstacle = self._store.get(project_id, parse_obstacle_id(text_id))
                if obstacle is not None and obstacle.seen < len(obstacle.entries):
                    self._store.put(replace(obstacle, seen=len(obstacle.entries)))


def serialise(obstacle: Obstacle) -> str:
    """The JSON text a store keeps for `obstacle`."""
    return json.dumps(obstacle_to_data(obstacle), sort_keys=True, separators=(",", ":"))


def deserialise(text: str) -> Obstacle:
    data = json.loads(text)
    assert isinstance(data, dict)  # noqa: S101  # reason: written by serialise
    return obstacle_from_data(data)
