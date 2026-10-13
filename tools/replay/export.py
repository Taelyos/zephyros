"""Turn the hub's SQLite database into a replay document (docs/replay-format.md).

    python -m tools.replay.export --db hub.db --project p_0123456789 \\
        --from-seq 238 --to-seq 501 --output run.replay.json

One replay tells what happened in one recorded run, in order. The exporter reads three sources:

* `hub-log`: the records of the project's stream of the hash-chained audit log. Every exported
  record is checked against `zephyros.core.audit.compute_hash` and against the link to the record
  before it; the result goes to `integrity`.
* `hub-state`: what the hub's tables hold and a record does not spell out — every contract version,
  the outcomes of a delivery, the observation of an inspection, the reasons of a review, the URL of
  a pull request, and the protected paths a delivery changes when the rule was not in force yet
  (`delivery.flag` with `by: "exporter"`).
* `transcript`: the answers of an instance's own conversation, from its Hermes state database.

What a replay never carries: the network address of each caller, the token identifiers, and the
prompts (the hub logs their length and digest only). Private IPv4 addresses — the ranges of
RFC 1918 and the carrier-grade range of RFC 6598 — and every `--redact` pattern are replaced by
`[redacted]`; `--rename` and `--rename-file` replace a real name by a neutral one wherever it
appears. The kinds that matched are listed in `run.redactions`, never the names themselves.

Only what the standard library and `zephyros` offer is imported: the exporter is a tool of the
repository, not a part of the hub.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zephyros.core.audit import compute_hash

FORMAT = "zephyros-replay"
VERSION = "1.0.0"
EXPORTER = f"tools.replay.export {VERSION}"
PLAYER_TEMPLATE = Path(__file__).with_name("player.html")
"""The self-contained player; `--html` writes a copy with the replay embedded in it."""
PLAYER_DATA = '<script type="application/json" id="replay-data"></script>'
REDACTED = "[redacted]"
DEFAULT_PROTECTED_PATHS = ("CONTRACT.md",)
DEFAULT_MESSAGE_CHARS = 1200
DEFAULT_PRICE_NOTE = "Rates given to the exporter on the command line; indicative only."
DEFAULT_INPUT_PRICE = 0.15
DEFAULT_OUTPUT_PRICE = 0.60
MAX_SUMMARY = 160
MAX_MESSAGE_PREVIEW = 100
MAX_SEQ = 2**63 - 1

SOURCE_LOG = "hub-log"
SOURCE_STATE = "hub-state"
SOURCE_TRANSCRIPT = "transcript"
OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"

PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)
"""The private ranges of RFC 1918 and the carrier-grade range of RFC 6598 (100.64.0.0/10)."""
PRIVATE_IPV4_KIND = "private IPv4 addresses"
RENAMED_KIND = "renamed names"
"""What `run.redactions` names when a `--rename` matched: never the name that was replaced."""

DROPPED_PARAMETERS = frozenset({"source", "token_id"})
"""What a replay never carries: the caller's address, and the identifiers of the tokens."""

VERSION_ACTIONS = frozenset({"contract.import", "contract.apply_reserved", "contract.approve"})
"""A record of one of these writes a new version of the contract, named in its `version_id`."""

CATEGORY_BY_ACTION: dict[str, str] = {
    "contract.import": "contract",
    "contract.apply_reserved": "contract",
    "contract.propose": "contract",
    "contract.approve": "contract",
    "contract.reject": "contract",
    "contract.pending": "contract",
    "contract.read": "contract",
    "acceptance.put": "contract",
    "instance.start": "turn",
    "turn.start": "turn",
    "turn.end": "turn",
    "gateway.call": "model",
    "delivery.push": "delivery",
    "delivery.declare": "delivery",
    "delivery.status": "delivery",
    "delivery.check": "delivery",
    "staging.prepare": "delivery",
    "staging.read": "delivery",
    "verification.run": "delivery",
    "pull_request.open": "delivery",
    "pull_request.comment": "delivery",
    "gate.decision": "gate",
    "inspection.record": "judgment",
    "delivery.review": "judgment",
}
DEFAULT_CATEGORY = "other"
CATEGORY_REFUSAL = "refusal"
CATEGORY_FLAG = "flag"
CATEGORY_MESSAGE = "message"

ACTOR_LABELS = {
    "operator": "Operator",
    "owner": "Owner",
    "contractor": "Contractor",
    "hub": "Hub",
    "main-agent": "Main agent",
}

_RECORDS = """
SELECT seq, time, principal, action, outcome, reason, parameters, previous, hash
FROM audit_records
WHERE stream = ? AND seq >= COALESCE(?, 0) AND seq <= COALESCE(?, ?)
ORDER BY seq
"""
_VERSIONS = """
SELECT number, version_id, objects, author, time
FROM contract_versions
WHERE project_id = ?
ORDER BY number
"""
_DELIVERIES = """
SELECT delivery_id, seq, body
FROM deliveries
WHERE project_id = ?
ORDER BY seq
"""
_PROJECT = "SELECT project_id, name, repository_url FROM projects WHERE project_id = ?"
_MESSAGES = """
SELECT session_id, role, content, timestamp
FROM messages
ORDER BY session_id, id
"""


class ReplayError(Exception):
    """The exporter cannot produce the replay (a missing file, an unusable option)."""


# ----------------------------------------------------------------------------- time and text
def parse_moment(text: str) -> datetime:
    """An ISO 8601 time, UTC. A time without a timezone is read as UTC."""
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def offset_ms(when: str, started: datetime) -> int:
    """Milliseconds between `when` and the start of the run."""
    return round((parse_moment(when) - started).total_seconds() * 1000)


def one_line(text: str) -> str:
    """A summary is one line, whatever the hub logged."""
    return " ".join(text.split())


def shorten(text: str, limit: int) -> str:
    collapsed = one_line(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "\u2026"


def actor_of(principal: str) -> str:
    """`owner:p_1234` is the owner of that project; anything else is its own name."""
    for prefix in ("owner:", "contractor:"):
        if principal.startswith(prefix):
            return prefix[:-1]
    return principal


def label(actor: str) -> str:
    return ACTOR_LABELS.get(actor, actor)


def kept(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters of a record, without what a replay never carries."""
    return {key: value for key, value in parameters.items() if key not in DROPPED_PARAMETERS}


# ----------------------------------------------------------------------------- redaction
def rename_pair(value: str) -> tuple[str, str]:
    """`OLD=NEW`, with both sides present and stripped of the space around the `=`."""
    old, separator, new = value.partition("=")
    old, new = old.strip(), new.strip()
    if not separator or not old or not new:
        raise ValueError(f"expected OLD=NEW, got {value!r}")
    return (old, new)


def read_renames(path: Path) -> list[tuple[str, str]]:
    """A file of renames: one `OLD=NEW` per line; a blank line and a `#` comment are ignored."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ReplayError(f"--rename-file {path}: {error}") from None
    renames: list[tuple[str, str]] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            renames.append(rename_pair(line))
        except ValueError as error:
            raise ReplayError(f"--rename-file {path}:{number}: {error}") from None
    return renames


def rename_pattern(old: str) -> re.Pattern[str]:
    """`alpha-svc` matches `Alpha-Svc` and `alpha-svc`, never `myalpha-svc`."""
    before = r"(?<!\w)" if re.fullmatch(r"\w", old[:1]) else ""
    after = r"(?!\w)" if re.fullmatch(r"\w", old[-1:]) else ""
    return re.compile(before + re.escape(old) + after, re.IGNORECASE)


@dataclass
class Redactor:
    """Replaces what a published replay must not carry, and remembers the kinds it replaced."""

    patterns: list[tuple[str, re.Pattern[str], str]] = field(default_factory=list)
    replaced: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, extra: Sequence[str], renames: Sequence[tuple[str, str]] = ()) -> Redactor:
        """`extra` are the `--redact` patterns; `renames` are the `--rename` pairs."""
        patterns: list[tuple[str, re.Pattern[str], str]] = [
            (PRIVATE_IPV4_KIND, PRIVATE_IPV4, REDACTED)
        ]
        for pattern in extra:
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise ReplayError(f"--redact {pattern!r}: {error}") from None
            patterns.append((f"pattern {pattern}", compiled, REDACTED))
        patterns.extend((RENAMED_KIND, rename_pattern(old), new) for old, new in renames)
        return cls(patterns)

    def text(self, value: str) -> str:
        for kind, pattern, replacement in self.patterns:
            value, count = pattern.subn(replacement, value)
            if count:
                self.replaced.add(kind)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


# ----------------------------------------------------------------------------- pricing
@dataclass(frozen=True, slots=True)
class Prices:
    """What a million tokens cost, from the command line; the replay says it is indicative."""

    input_per_million: float
    output_per_million: float
    note: str

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            prompt_tokens * self.input_per_million / 1_000_000
            + completion_tokens * self.output_per_million / 1_000_000,
            6,
        )

    def document(self) -> dict[str, Any]:
        return {
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
            "currency": "USD",
            "note": self.note,
        }


# ----------------------------------------------------------------------------- the hub
@dataclass(frozen=True, slots=True)
class LogRecord:
    """One record of the hash-chained log, with the fields the chain is built from."""

    seq: int
    time: str
    principal: str
    action: str
    outcome: str
    reason: str
    parameters: Mapping[str, Any]
    parameters_text: str
    """The canonical JSON the record was hashed over, kept as text so the check is exact."""

    previous: str
    hash: str


def connect_read_only(path: Path) -> sqlite3.Connection:
    """A connection that can only read: the hub's own files are never written by the exporter."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


class Hub:
    """A read-only view of a hub database: one stream of the log, the contract, the deliveries."""

    def __init__(self, path: Path) -> None:
        self._db = connect_read_only(path)

    def __enter__(self) -> Hub:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def records(self, stream: str, from_seq: int | None, to_seq: int | None) -> list[LogRecord]:
        rows = self._db.execute(_RECORDS, (stream, from_seq, to_seq, MAX_SEQ)).fetchall()
        return [
            LogRecord(
                seq=int(row["seq"]),
                time=str(row["time"]),
                principal=str(row["principal"]),
                action=str(row["action"]),
                outcome=str(row["outcome"]),
                reason=str(row["reason"]),
                parameters=json.loads(row["parameters"]),
                parameters_text=str(row["parameters"]),
                previous=str(row["previous"]),
                hash=str(row["hash"]),
            )
            for row in rows
        ]

    def project(self, project_id: str) -> dict[str, Any] | None:
        row = self._db.execute(_PROJECT, (project_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["project_id"]),
            "name": str(row["name"]),
            "repository": None if row["repository_url"] is None else str(row["repository_url"]),
        }

    def versions(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(_VERSIONS, (project_id,)).fetchall()
        return [
            {
                "number": int(row["number"]),
                "version_id": str(row["version_id"]),
                "objects": json.loads(row["objects"]),
                "author": str(row["author"]),
                "time": str(row["time"]),
            }
            for row in rows
        ]

    def deliveries(self, project_id: str) -> dict[str, dict[str, Any]]:
        rows = self._db.execute(_DELIVERIES, (project_id,)).fetchall()
        return {str(row["delivery_id"]): json.loads(row["body"]) for row in rows}


def read_transcript(path: Path) -> list[tuple[str, str, str, float]]:
    """The messages of an instance's conversation: (session, role, content, time)."""
    db = connect_read_only(path)
    try:
        rows = db.execute(_MESSAGES).fetchall()
    finally:
        db.close()
    return [
        (
            str(row["session_id"]),
            str(row["role"]),
            "" if row["content"] is None else str(row["content"]),
            float(row["timestamp"]),
        )
        for row in rows
    ]


# ----------------------------------------------------------------------------- integrity
def verify_chain(stream: str, records: Sequence[LogRecord]) -> dict[str, Any]:
    """Check every exported record against its hash and the link to the one before it."""
    detail: str | None = None
    for index, record in enumerate(records):
        expected = compute_hash(
            seq=record.seq,
            time=record.time,
            stream=stream,
            principal=record.principal,
            action=record.action,
            outcome=record.outcome,
            reason=record.reason,
            parameters=record.parameters_text,
            previous=record.previous,
        )
        if expected != record.hash:
            detail = detail or f"record {record.seq} does not match its hash"
        elif index and record.previous != records[index - 1].hash:
            detail = detail or (
                f"record {record.seq} does not link to record {records[index - 1].seq}"
            )
    return {
        "chain": "broken" if detail else "verified",
        "records": len(records),
        "first_previous": records[0].previous if records else "",
        "last_hash": records[-1].hash if records else "",
        "detail": detail,
    }


# ----------------------------------------------------------------------------- contract objects
def contract_object(key: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """One contract object in the shape the format names; a field an object lacks is null."""
    methods = item.get("methods")
    if item.get("verification") is not None:
        verification: object = item["verification"]
    elif isinstance(methods, (list, tuple)) and methods:
        verification = ",".join(str(method) for method in methods)
    else:
        verification = None
    return {
        "id": item.get("id", key),
        "type": item.get("type"),
        "title": item.get("title"),
        "text": item.get("text"),
        "priority": item.get("priority"),
        "verification": verification,
    }


def contract_objects(objects: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every object of a version, in the order of its identifier."""
    return [
        contract_object(str(key), item)
        for key, item in sorted(objects.items())
        if isinstance(item, Mapping)
    ]


def protected_paths_in(changed: Sequence[str], protected: Sequence[str]) -> list[str]:
    """The changed paths that are the client's own files: a protected path, or under one."""
    return [
        path
        for path in changed
        if any(path == rule or (rule.endswith("/") and path.startswith(rule)) for rule in protected)
    ]


# ----------------------------------------------------------------------------- events
@dataclass(slots=True)
class Event:
    """One line of the replay. Built, then sorted, dated, numbered and rendered."""

    when: str
    sequence: int | None
    source: str
    actor: str
    action: str
    category: str
    outcome: str
    summary: str
    data: dict[str, Any]
    order: int = 0

    def render(self, identifier: int, started: datetime) -> dict[str, Any]:
        return {
            "id": identifier,
            "t": self.when,
            "offset_ms": offset_ms(self.when, started),
            "seq": self.sequence,
            "source": self.source,
            "actor": self.actor,
            "type": self.action,
            "category": self.category,
            "outcome": self.outcome,
            "summary": self.summary,
            "data": self.data,
        }


def delivery_id_of(parameters: Mapping[str, Any]) -> str:
    """The delivery a record names, as `delivery` (the gate) or `delivery_id` (the hub)."""
    value = parameters.get("delivery") or parameters.get("delivery_id")
    return "" if value is None else str(value)


def _refusal_event(record: LogRecord, actor: str) -> Event:
    parameters = kept(record.parameters)
    data: dict[str, Any] = {**parameters, "reason": record.reason, "action": record.action}
    data.setdefault("detail", None)
    detail = parameters.get("detail") or record.action
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=record.action,
        category=CATEGORY_REFUSAL,
        outcome=record.outcome,
        summary=shorten(f"Refused ({record.reason}): {detail}", MAX_SUMMARY),
        data=data,
    )


def _model_event(record: LogRecord, actor: str, prices: Prices) -> Event:
    parameters = kept(record.parameters)
    model = parameters.pop("model", None)
    data: dict[str, Any] = {"side": actor, "model": model}
    if record.outcome == OUTCOME_OK:
        prompt = int(parameters.get("prompt_tokens") or 0)
        completion = int(parameters.get("completion_tokens") or 0)
        tool_calls = int(parameters.get("tool_calls") or 0)
        data.update(
            {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "tool_calls": tool_calls,
                "cost_usd": prices.cost(prompt, completion),
            }
        )
        plural = "" if tool_calls == 1 else "s"
        summary = f"Model call: {prompt:,} in / {completion:,} out, {tool_calls} tool call{plural}"
    else:
        data["detail"] = parameters.get("detail")
        summary = f"Model call failed: {parameters.get('detail') or record.action}"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=record.action,
        category="model",
        outcome=record.outcome,
        summary=shorten(summary, MAX_SUMMARY),
        data=data,
    )


def _contract_event(record: LogRecord, actor: str, version_numbers: Mapping[str, int]) -> Event:
    data = kept(record.parameters)
    action = record.action
    text = shorten(str(data.get("summary") or ""), MAX_SUMMARY)
    if action == "contract.read":
        version_id = str(data.get("version_id") or "?")
        number = version_numbers.get(version_id)
        if number is not None:
            data["version"] = number
        named = number if number is not None else version_id
        summary = f"{label(actor)} reads contract version {named}"
    elif action == "contract.propose":
        change = str(data.get("change_id") or "?")
        summary = f"{label(actor)} proposes change {change}: {text}"
    elif action == "contract.approve":
        summary = (
            f"{label(actor)} approves change {data.get('change_id', '?')} "
            f"as {data.get('version_id', '?')}"
        )
    elif action == "contract.reject":
        summary = f"{label(actor)} rejects change {data.get('change_id', '?')}"
    elif action == "contract.pending":
        summary = f"{label(actor)} lists the pending changes ({data.get('pending', 0)})"
    elif action == "contract.import":
        summary = (
            f"{label(actor)} imports contract {data.get('version_id', '?')} "
            f"({data.get('objects', 0)} objects)"
        )
    elif action == "contract.apply_reserved":
        summary = f"{label(actor)} applies a reserved change as {data.get('version_id', '?')}"
    elif action == "acceptance.put":
        summary = f"{label(actor)} stores acceptance test {data.get('path', '?')}"
    else:
        summary = f"{label(actor)} {action}"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=action,
        category="contract",
        outcome=record.outcome,
        summary=shorten(summary, MAX_SUMMARY),
        data=data,
    )


def _turn_event(record: LogRecord, actor: str) -> Event:
    data = kept(record.parameters)
    who = label(str(data.get("side") or actor))
    if record.action == "instance.start":
        summary = (
            f"{label(actor)} starts the {who} instance ({data.get('prompt_chars', 0)} prompt chars)"
        )
    elif record.action == "turn.start":
        summary = f"{who} turn begins ({data.get('prompt_chars', 0)} prompt chars)"
    else:
        summary = f"{who} turn ends: {data.get('result', '?')}"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=record.action,
        category="turn",
        outcome=record.outcome,
        summary=shorten(summary, MAX_SUMMARY),
        data=data,
    )


def _delivery_event(
    record: LogRecord, actor: str, deliveries: Mapping[str, Mapping[str, Any]]
) -> Event:
    data = kept(record.parameters)
    action = record.action
    delivery_id = delivery_id_of(record.parameters)
    body = deliveries.get(delivery_id, {})
    data.pop("delivery", None)
    if delivery_id:
        data["delivery_id"] = delivery_id
    if body:
        if body.get("branch") is not None:
            data.setdefault("branch", body["branch"])
        if body.get("changed_paths"):
            data["changed_paths"] = list(body["changed_paths"])
        if body.get("pull_request"):
            data.setdefault("url", body["pull_request"])
    if action == "delivery.push":
        summary = (
            f"{label(actor)} pushes {delivery_id} on {data.get('branch', '?')} "
            f"({data.get('changed', 0)} paths)"
        )
    elif action == "delivery.declare":
        summary = f"{label(actor)} declares delivery {delivery_id}"
    elif action == "delivery.status":
        if body:
            data["state"] = body.get("state")
            data["decision"] = body.get("decision")
        summary = f"{label(actor)} reads the state of {delivery_id}"
    elif action == "delivery.check":
        summary = f"Hub checks delivery {delivery_id} ({data.get('flagged', 0)} flagged paths)"
    elif action == "staging.prepare":
        if record.outcome == OUTCOME_OK:
            summary = (
                f"Hub prepares staging for {data.get('repository', '?')} "
                f"{data.get('branch', '?')} at {data.get('base', '?')}"
            )
        else:
            summary = f"Hub cannot prepare staging: {data.get('error', '?')}"
    elif action == "staging.read":
        summary = f"{label(actor)} reads the staging area ({data.get('bytes', 0)} bytes)"
    elif action == "verification.run":
        summary = (
            f"Verification: {data.get('passed', 0)}/{data.get('tests', 0)} acceptance tests "
            f"passed for {delivery_id}"
        )
    elif action == "pull_request.open":
        summary = f"Hub opens pull request #{data.get('number')} for {delivery_id}"
    else:
        summary = f"Hub comments on pull request #{data.get('number')} for {delivery_id}"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=action,
        category="delivery",
        outcome=record.outcome,
        summary=shorten(summary, MAX_SUMMARY),
        data=data,
    )


def _gate_event(
    record: LogRecord, actor: str, deliveries: Mapping[str, Mapping[str, Any]]
) -> Event:
    data = kept(record.parameters)
    delivery_id = delivery_id_of(record.parameters)
    data["delivery_id"] = delivery_id
    data.pop("delivery", None)
    outcomes = deliveries.get(delivery_id, {}).get("outcomes")
    if outcomes:
        data["outcomes"] = dict(outcomes)
    decision = str(data.get("decision") or "?")
    version = data.get("contract_version")
    suffix = "" if version is None else f" (contract v{version})"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=record.action,
        category="gate",
        outcome=record.outcome,
        summary=shorten(f"Gate: {decision} for {delivery_id}{suffix}", MAX_SUMMARY),
        data=data,
    )


def _judgment_event(
    record: LogRecord, actor: str, deliveries: Mapping[str, Mapping[str, Any]]
) -> Event:
    data = kept(record.parameters)
    delivery_id = delivery_id_of(record.parameters)
    data["delivery_id"] = delivery_id
    body = deliveries.get(delivery_id, {})
    if record.action == "inspection.record":
        object_id = str(data.get("object_id") or "?")
        observations = body.get("inspections") or {}
        found = observations.get(object_id)
        if isinstance(found, Mapping) and found.get("observation") is not None:
            data["observation"] = str(found["observation"])
        summary = (
            f"{label(actor)} records the inspection of {object_id} on {delivery_id}: "
            f"{data.get('verdict', '?')}"
        )
    else:
        review = body.get("review")
        if isinstance(review, Mapping) and review.get("reasons") is not None:
            data["reasons"] = str(review["reasons"])
        summary = f"{label(actor)} reviews {delivery_id}: {data.get('verdict', '?')}"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=record.action,
        category="judgment",
        outcome=record.outcome,
        summary=shorten(summary, MAX_SUMMARY),
        data=data,
    )


def _other_event(record: LogRecord, actor: str) -> Event:
    data = kept(record.parameters)
    action = record.action
    if action == "token.issue":
        summary = f"Hub issues a token to the {data.get('side', '?')} side"
    elif action == "notification.sent":
        summary = (
            f"Hub notifies the operator: {data.get('event', action)} "
            f"for {data.get('delivery_id', '?')} ({data.get('decision', '?')})"
        )
    else:
        summary = f"{label(actor)} {action}"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=action,
        category=CATEGORY_BY_ACTION.get(action, DEFAULT_CATEGORY),
        outcome=record.outcome,
        summary=shorten(summary, MAX_SUMMARY),
        data=data,
    )


def hub_log_event(
    record: LogRecord,
    deliveries: Mapping[str, Mapping[str, Any]],
    version_numbers: Mapping[str, int],
    prices: Prices,
) -> Event:
    """One record of the log as one event of the replay."""
    actor = actor_of(record.principal)
    if record.outcome == OUTCOME_REFUSED:
        return _refusal_event(record, actor)
    category = CATEGORY_BY_ACTION.get(record.action, DEFAULT_CATEGORY)
    if category == "model":
        return _model_event(record, actor, prices)
    if category == "contract":
        return _contract_event(record, actor, version_numbers)
    if category == "turn":
        return _turn_event(record, actor)
    if category == "delivery":
        return _delivery_event(record, actor, deliveries)
    if category == "gate":
        return _gate_event(record, actor, deliveries)
    if category == "judgment":
        return _judgment_event(record, actor, deliveries)
    return _other_event(record, actor)


def version_event(version: Mapping[str, Any]) -> Event:
    """A contract version, read from the hub's table: the contract as it stood at that time."""
    author = str(version["author"])
    objects = version["objects"]
    data = {
        "number": version["number"],
        "version_id": version["version_id"],
        "author": author,
        "objects": contract_objects(objects if isinstance(objects, Mapping) else {}),
    }
    return Event(
        when=str(version["time"]),
        sequence=None,
        source=SOURCE_STATE,
        actor=actor_of(author),
        action="contract.version",
        category="contract",
        outcome=OUTCOME_OK,
        summary=f"Contract version {version['number']} ({author})",
        data=data,
    )


def flagged_event(
    delivery_id: str, body: Mapping[str, Any], protected: Sequence[str]
) -> Event | None:
    """The protected paths a delivery changes: what the hub flagged, or what the exporter finds.

    The hub flags them (`by: "hub"`) when its own rule was in force; a run older than the rule
    carries none, and the exporter derives them from the delivered paths (`by: "exporter"`).
    """
    received = body.get("received_at")
    if received is None:
        return None
    announced = [str(path) for path in body.get("flagged_paths") or ()]
    if announced:
        by, paths = "hub", announced
    else:
        changed = [str(path) for path in body.get("changed_paths") or ()]
        paths = protected_paths_in(changed, protected)
        by = "exporter"
    if not paths:
        return None
    return Event(
        when=str(received),
        sequence=None,
        source=SOURCE_STATE,
        actor="hub",
        action="delivery.flag",
        category=CATEGORY_FLAG,
        outcome=OUTCOME_OK,
        summary=f"Delivery {delivery_id} changes a protected path: {', '.join(paths)}",
        data={"delivery_id": delivery_id, "paths": paths, "rule": "protected-path", "by": by},
    )


def transcript_events(
    side: str,
    messages: Sequence[tuple[str, str, str, float]],
    window: tuple[datetime, datetime] | None,
    message_chars: int,
) -> list[Event]:
    """The answers of one instance: an assistant message that says something, inside the run."""
    if window is None:
        return []
    by_session: dict[str, list[tuple[str, str, str, float]]] = {}
    for session, role, text, timestamp in messages:
        by_session.setdefault(session, []).append((session, role, text, timestamp))
    events: list[Event] = []
    for session in sorted(by_session):
        conversation = by_session[session]
        for index, (_, role, text, timestamp) in enumerate(conversation):
            if role != "assistant" or not text.strip():
                continue
            moment = datetime.fromtimestamp(timestamp, UTC)
            if not window[0] <= moment <= window[1]:
                continue
            following = conversation[index + 1] if index + 1 < len(conversation) else None
            place = "final" if following is None or following[1] == "user" else "note"
            body = one_line(text)
            truncated = len(body) > message_chars
            events.append(
                Event(
                    when=moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    sequence=None,
                    source=SOURCE_TRANSCRIPT,
                    actor=side,
                    action="agent.message",
                    category=CATEGORY_MESSAGE,
                    outcome=OUTCOME_OK,
                    summary=shorten(
                        f"{label(side)} {'final answer' if place == 'final' else 'note'}: "
                        f"{body[:MAX_MESSAGE_PREVIEW]}",
                        MAX_SUMMARY,
                    ),
                    data={
                        "side": side,
                        "text": body[:message_chars] if truncated else body,
                        "truncated": truncated,
                        "role": place,
                    },
                )
            )
    return events


# ----------------------------------------------------------------------------- totals
def totals_of(events: Sequence[Event], started: datetime, ended: datetime) -> dict[str, Any]:
    model_calls: dict[str, int] = {}
    tokens: dict[str, dict[str, int]] = {}
    cost_usd: dict[str, float] = {}
    refusals: dict[str, int] = {}
    decisions: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    for event in events:
        if event.category == "model":
            side = str(event.data.get("side") or event.actor)
            model_calls[side] = model_calls.get(side, 0) + 1
            counted = tokens.setdefault(side, {"input": 0, "output": 0})
            counted["input"] += int(event.data.get("prompt_tokens") or 0)
            counted["output"] += int(event.data.get("completion_tokens") or 0)
            cost = float(event.data.get("cost_usd") or 0)
            cost_usd[side] = round(cost_usd.get(side, 0.0) + cost, 6)
        elif event.category == CATEGORY_REFUSAL:
            refusals[event.actor] = refusals.get(event.actor, 0) + 1
        if event.action == "gate.decision":
            decisions.append(
                {
                    "delivery_id": event.data.get("delivery_id"),
                    "decision": event.data.get("decision"),
                    "t": event.when,
                }
            )
        elif event.action == "delivery.review" and event.data.get("verdict"):
            verdicts.append(
                {
                    "delivery_id": event.data.get("delivery_id"),
                    "verdict": event.data.get("verdict"),
                    "t": event.when,
                }
            )
    sides = sorted(set(model_calls) | set(refusals))
    return {
        "events": len(events),
        "model_calls": {side: model_calls.get(side, 0) for side in sides},
        "tokens": {side: tokens.get(side, {"input": 0, "output": 0}) for side in sides},
        "cost_usd": {
            **{side: cost_usd.get(side, 0.0) for side in sides},
            "total": round(float(sum(cost_usd.values())), 6),
        },
        "refusals": {side: refusals.get(side, 0) for side in sides},
        "decisions": decisions,
        "verdicts": verdicts,
        "duration_ms": round((ended - started).total_seconds() * 1000),
    }


def empty_totals(events: Sequence[Event]) -> dict[str, Any]:
    return {
        "events": len(events),
        "model_calls": {},
        "tokens": {},
        "cost_usd": {"total": 0.0},
        "refusals": {},
        "decisions": [],
        "verdicts": [],
        "duration_ms": 0,
    }


# ----------------------------------------------------------------------------- the document
@dataclass(frozen=True, slots=True)
class Options:
    """Everything the exporter needs, as the command line gives it."""

    db: Path
    project: str
    kind: str = "real"
    attacks: tuple[str, ...] = ()
    title: str | None = None
    model: str | None = None
    from_seq: int | None = None
    to_seq: int | None = None
    transcripts: tuple[tuple[str, Path], ...] = ()
    redact: tuple[str, ...] = ()
    renames: tuple[tuple[str, str], ...] = ()
    rename_file: Path | None = None
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    message_chars: int = DEFAULT_MESSAGE_CHARS
    input_price: float = DEFAULT_INPUT_PRICE
    output_price: float = DEFAULT_OUTPUT_PRICE
    price_note: str = DEFAULT_PRICE_NOTE


def replay_document(options: Options, exported_at: str | None = None) -> dict[str, Any]:
    """Build the whole replay document for one project and one range of its log."""
    prices = Prices(options.input_price, options.output_price, options.price_note)
    renames = list(options.renames)
    if options.rename_file is not None:
        renames.extend(read_renames(options.rename_file))
    redactor = Redactor.of(options.redact, renames)
    produced = exported_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with Hub(options.db) as hub:
        project = hub.project(options.project)
        if project is None:
            raise ReplayError(f"there is no project {options.project} in {options.db}")
        records = hub.records(options.project, options.from_seq, options.to_seq)
        versions = hub.versions(options.project)
        deliveries = hub.deliveries(options.project)
        integrity = verify_chain(options.project, records)
        version_numbers = {str(item["version_id"]): int(item["number"]) for item in versions}
        window = (
            (parse_moment(records[0].time), parse_moment(records[-1].time)) if records else None
        )
        events = [hub_log_event(record, deliveries, version_numbers, prices) for record in records]
        named_versions = {
            str(record.parameters["version_id"])
            for record in records
            if record.action in VERSION_ACTIONS and record.parameters.get("version_id")
        }
        events.extend(
            version_event(version)
            for version in versions
            if within(str(version["time"]), window) or str(version["version_id"]) in named_versions
        )
        for delivery_id, body in deliveries.items():
            flagged = flagged_event(delivery_id, body, options.protected_paths)
            if flagged is not None and within(flagged.when, window):
                events.append(flagged)
        for side, path in options.transcripts:
            events.extend(
                transcript_events(side, read_transcript(path), window, options.message_chars)
            )
    events.sort(
        key=lambda event: (
            parse_moment(event.when),
            0 if event.sequence is not None else 1,
            event.sequence if event.sequence is not None else 0,
            event.order,
        )
    )
    events = [redact_event(event, redactor) for event in events]
    started = parse_moment(events[0].when) if events else None
    ended = parse_moment(events[-1].when) if events else None
    return {
        "format": FORMAT,
        "version": VERSION,
        "run": {
            "kind": options.kind,
            "title": redactor.text(options.title or str(project["name"])),
            "attacks": list(options.attacks) if options.kind == "scripted" else [],
            "project": redactor.value(project),
            "stream": options.project,
            "from_seq": records[0].seq if records else (options.from_seq or 0),
            "to_seq": records[-1].seq if records else (options.to_seq or 0),
            "started_at": events[0].when if events else "",
            "ended_at": events[-1].when if events else "",
            "model": options.model,
            "exported_at": produced,
            "exporter": EXPORTER,
            "redactions": sorted(redactor.replaced),
        },
        "integrity": integrity,
        "pricing": prices.document(),
        "contract": redactor.value(contract_of(versions, ended)),
        "events": [event.render(index, started) for index, event in enumerate(events)]
        if started
        else [],
        "totals": totals_of(events, started, ended)
        if started is not None and ended is not None
        else empty_totals(events),
    }


def within(when: str, window: tuple[datetime, datetime] | None) -> bool:
    """A hub-state event belongs to the replay only when it happened inside the exported range."""
    if window is None:
        return False
    moment = parse_moment(when)
    return window[0] <= moment <= window[1]


def redact_event(event: Event, redactor: Redactor) -> Event:
    """Replace what must not be published, in the summary and in the details."""
    event.summary = redactor.text(event.summary)
    event.data = redactor.value(event.data)
    return event


def contract_of(versions: Sequence[Mapping[str, Any]], ended: datetime | None) -> dict[str, Any]:
    """The contract as it stood at the end of the exported range."""
    chosen: Mapping[str, Any] | None = None
    for version in versions:
        if ended is None or parse_moment(str(version["time"])) <= ended:
            chosen = version
    if chosen is None:
        return {"version": None, "version_id": None, "objects": []}
    objects = chosen["objects"]
    return {
        "version": chosen["number"],
        "version_id": chosen["version_id"],
        "objects": contract_objects(objects if isinstance(objects, Mapping) else {}),
    }


def document_text(document: Mapping[str, Any]) -> str:
    """The deterministic JSON text of a replay."""
    return json.dumps(document, indent=1, ensure_ascii=False, sort_keys=False)


# ----------------------------------------------------------------------------- the player
def player_document(template: str, payload: str) -> str:
    """The player with the replay inside it, every `</` of the JSON escaped."""
    if template.count(PLAYER_DATA) != 1:
        raise ReplayError(f"the player must hold exactly one {PLAYER_DATA!r} element")
    escaped = payload.replace("</", "<\\/")
    inner = f'<script type="application/json" id="replay-data">{escaped}</script>'
    return template.replace(PLAYER_DATA, inner)


def write_player(path: Path, payload: str) -> None:
    if not PLAYER_TEMPLATE.is_file():
        raise ReplayError(f"the player template is missing: {PLAYER_TEMPLATE}")
    template = PLAYER_TEMPLATE.read_text(encoding="utf-8")
    path.write_text(player_document(template, payload), encoding="utf-8")


# ----------------------------------------------------------------------------- the command line
def parse_rename(value: str) -> tuple[str, str]:
    try:
        return rename_pair(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"--rename {error}") from None


def parse_transcript(value: str) -> tuple[str, Path]:
    side, separator, path = value.partition("=")
    if not separator or side not in ("owner", "contractor") or not path:
        raise argparse.ArgumentTypeError(
            f"--transcript {value!r}: expected SIDE=PATH, with SIDE in owner|contractor"
        )
    return (side, Path(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.replay.export",
        description="Export a replay document from the hub's SQLite database.",
    )
    parser.add_argument("--db", required=True, type=Path, help="the hub's SQLite file")
    parser.add_argument("--project", required=True, help="the project identifier (the stream)")
    parser.add_argument("--kind", choices=("real", "scripted"), default="real")
    parser.add_argument("--attack", dest="attacks", action="append", metavar="ATK-ID")
    parser.add_argument("--title", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--from-seq", dest="from_seq", type=int, default=None)
    parser.add_argument("--to-seq", dest="to_seq", type=int, default=None)
    parser.add_argument(
        "--transcript",
        dest="transcripts",
        action="append",
        type=parse_transcript,
        metavar="SIDE=PATH",
    )
    parser.add_argument("--redact", dest="redact", action="append", metavar="REGEX")
    parser.add_argument(
        "--rename",
        dest="renames",
        action="append",
        type=parse_rename,
        metavar="OLD=NEW",
        help="replace a name wherever it appears in the replay (repeatable)",
    )
    parser.add_argument(
        "--rename-file",
        dest="rename_file",
        type=Path,
        default=None,
        metavar="PATH",
        help="a file of OLD=NEW lines ('#' comments) to replace names",
    )
    parser.add_argument(
        "--input-price", dest="input_price", type=float, default=DEFAULT_INPUT_PRICE
    )
    parser.add_argument(
        "--output-price", dest="output_price", type=float, default=DEFAULT_OUTPUT_PRICE
    )
    parser.add_argument("--price-note", dest="price_note", default=DEFAULT_PRICE_NOTE)
    parser.add_argument("--protected-path", dest="protected_paths", action="append", metavar="PATH")
    parser.add_argument(
        "--message-chars", dest="message_chars", type=int, default=DEFAULT_MESSAGE_CHARS
    )
    parser.add_argument("--output", type=Path, default=None, help="write here (stdout by default)")
    parser.add_argument(
        "--html", type=Path, default=None, help="also write the player with the replay in it"
    )
    return parser


def options_of(arguments: argparse.Namespace) -> Options:
    return Options(
        db=arguments.db,
        project=arguments.project,
        kind=arguments.kind,
        attacks=tuple(arguments.attacks or ()),
        title=arguments.title,
        model=arguments.model,
        from_seq=arguments.from_seq,
        to_seq=arguments.to_seq,
        transcripts=tuple(arguments.transcripts or ()),
        redact=tuple(arguments.redact or ()),
        renames=tuple(arguments.renames or ()),
        rename_file=arguments.rename_file,
        protected_paths=(
            tuple(arguments.protected_paths)
            if arguments.protected_paths
            else DEFAULT_PROTECTED_PATHS
        ),
        message_chars=arguments.message_chars,
        input_price=arguments.input_price,
        output_price=arguments.output_price,
        price_note=arguments.price_note,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        text = document_text(replay_document(options_of(arguments)))
        if arguments.html is not None:
            write_player(arguments.html, text)
        if arguments.output is None:
            print(text)
        else:
            arguments.output.write_text(text + "\n", encoding="utf-8")
    except ReplayError as error:
        print(f"replay export: {error}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"replay export: cannot read {arguments.db}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
