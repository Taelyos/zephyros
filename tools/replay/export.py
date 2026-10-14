"""Turn the hub's SQLite database into a replay document (docs/replay-format.md).

    python -m tools.replay.export --db hub.db --project p_0123456789 \\
        --from-seq 238 --to-seq 501 --output run.replay.json

One replay tells what happened in one recorded run, in order. The exporter reads three sources:

* `hub-log`: the records of the project's stream of the hash-chained audit log. Every exported
  record is checked against `zephyros.core.audit.compute_hash` and against the link to the record
  before it; the result goes to `integrity`. The `data` of such an event holds what its record
  holds, and nothing the tables add.
* `hub-state`: what the hub's tables hold and a record does not spell out. Every contract version
  is an event of its own, and so is a protected path that a delivery changed when the hub did not
  flag such paths yet (`delivery.flag`, `by: "exporter"`). What the tables add to a record — the
  paths a delivery changed, the outcomes a decision was computed from, the observation of an
  inspection, the reasons of a review, the URL of a pull request — goes to the event's
  `hub_state`, and only when it held at the time of the event: the tables keep the last state of
  a delivery, not its history.
* `transcript`: the answers of an instance's own conversation, from its Hermes state database.

What a replay never carries: the network address of each caller, the token identifiers, and the
prompts (the hub logs their length and digest only). Token-shaped secrets are masked the way the
hub masks its log (EXG-49). Private IPv4 addresses — the ranges of RFC 1918 and the carrier-grade
range of RFC 6598 — and every `--redact` pattern are replaced by `[redacted]`; `--rename` and
`--rename-file` replace a real name by a neutral one. All of it applies to every text of the
replay, dictionary keys included. The kinds that matched are listed in `run.redactions`, never
what was replaced.

Only what the standard library and `zephyros` offer is imported: the exporter is a tool of the
repository, not a part of the hub.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zephyros.core.audit import compute_hash
from zephyros.core.deliveries import flagged_paths
from zephyros.core.masking import mask_text

FORMAT = "zephyros-replay"
VERSION = "2.0.0"
EXPORTER = f"tools.replay.export {VERSION}"
KINDS = ("real", "scripted", "synthetic")
PLAYER_TEMPLATE = Path(__file__).with_name("player.html")
"""The self-contained player; `--html` writes a copy with the replay embedded in it."""
PLAYER_DATA = '<script type="application/json" id="replay-data"></script>'
REDACTED = "[redacted]"
DEFAULT_PROTECTED_PATHS = ("CONTRACT.md",)
DEFAULT_MESSAGE_CHARS = 1200
DEFAULT_INPUT_PRICE = 0.15
DEFAULT_OUTPUT_PRICE = 0.60
DEFAULT_PRICE_NOTE = (
    "The exporter's default rates, not read from any provider's price list; indicative only."
)
GIVEN_PRICE_NOTE = "Rates given to the exporter on the command line; indicative only."
MAX_SUMMARY = 160
MAX_MESSAGE_PREVIEW = 100
MAX_SEQ = 2**63 - 1

SOURCE_LOG = "hub-log"
SOURCE_STATE = "hub-state"
SOURCE_TRANSCRIPT = "transcript"
OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"
OUTCOME_ERROR = "error"

PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)
"""The private ranges of RFC 1918 and the carrier-grade range of RFC 6598 (100.64.0.0/10)."""
PRIVATE_IPV4_KIND = "private IPv4 addresses"
MASKED_KIND = "token-shaped secrets"
PATTERN_KIND = "--redact patterns"
RENAMED_KIND = "renamed names"
"""What `run.redactions` names: the kind of what was replaced, never what was replaced."""
FREE_MAPS = frozenset({"outcomes", "flagged"})
"""The dictionaries whose keys are data — test identifiers, contract objects — rather than the
names of fields. Their keys are texts like any other; the names of fields are never changed."""

DROPPED_PARAMETERS = frozenset({"source", "token_id"})
"""What a replay never carries: the caller's address, and the identifiers of the tokens."""

VERSION_ACTIONS = frozenset({"contract.import", "contract.apply_reserved", "contract.approve"})
"""A record of one of these writes a new version of the contract, named in its `version_id`."""
VERSION_ID = re.compile(r"v(\d+)-")
"""A version identifier starts with its number: `v2-0123456789ab`."""
PULL_REQUEST_ACTIONS = frozenset(
    {"pull_request.open", "pull_request.comment", "pull_request.retitle"}
)

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
    "delivery.check": "flag",
    "staging.prepare": "delivery",
    "staging.read": "delivery",
    "verification.run": "delivery",
    "pull_request.open": "delivery",
    "pull_request.comment": "delivery",
    "pull_request.retitle": "delivery",
    "gate.decision": "gate",
    "inspection.record": "judgment",
    "delivery.review": "judgment",
}
"""A `delivery.check` the hub accepts is always a flag: it records one only when a delivery
changes a protected path. One it refuses is a refusal, as every refused action."""
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
    return collapsed[: limit - 1].rstrip() + "…"


def actor_of(principal: str) -> str:
    """`owner:p_1234` is the owner of that project; anything else is its own name."""
    for prefix in ("owner:", "contractor:"):
        if principal.startswith(prefix):
            return prefix[:-1]
    return principal


def label(actor: str) -> str:
    return ACTOR_LABELS.get(actor, actor)


def number(value: object) -> str:
    """An integer with its thousands separated, anything else as it is: `18,432`, `?`."""
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def plural(count: object, word: str) -> str:
    """`1 path`, `2 paths`."""
    return f"{number(count)} {word}" if count == 1 else f"{number(count)} {word}s"


def kept(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters of a record, without what a replay never carries. The gate and the worker
    name a delivery `delivery`, the tools `delivery_id`: a replay always says `delivery_id`."""
    data = {key: value for key, value in parameters.items() if key not in DROPPED_PARAMETERS}
    if "delivery" in data and "delivery_id" not in data:
        data["delivery_id"] = data.pop("delivery")
    return data


def version_number(version_id: object) -> int | None:
    """The number a version identifier starts with, or None for anything else."""
    found = VERSION_ID.match(str(version_id or ""))
    return int(found.group(1)) if found else None


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
    """`alpha-svc` matches `Alpha-Svc`, `alpha-svc` and the middle of `test_alpha-svc_up`, never
    `myalpha-svc`: a name stops where letters and digits stop, and `_` is not one of them."""
    letter_or_digit = r"[^\W_]"
    before = rf"(?<!{letter_or_digit})" if re.fullmatch(letter_or_digit, old[:1]) else ""
    after = rf"(?!{letter_or_digit})" if re.fullmatch(letter_or_digit, old[-1:]) else ""
    return re.compile(before + re.escape(old) + after, re.IGNORECASE)


def distinct(key: str, taken: Mapping[str, object]) -> str:
    """A key that redaction made equal to one already there gets a number, so no value is lost:
    `[redacted]`, then `[redacted] (2)`."""
    if key not in taken:
        return key
    number = 2
    while f"{key} ({number})" in taken:
        number += 1
    return f"{key} ({number})"


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
            patterns.append((PATTERN_KIND, compiled, REDACTED))
        patterns.extend((RENAMED_KIND, rename_pattern(old), new) for old, new in renames)
        return cls(patterns)

    def text(self, value: str) -> str:
        masked = mask_text(value)
        if masked != value:
            self.replaced.add(MASKED_KIND)
        for kind, pattern, replacement in self.patterns:
            masked, count = pattern.subn(replacement, masked)
            if count:
                self.replaced.add(kind)
        return masked

    def value(self, value: Any, *, free_keys: bool = False) -> Any:
        """Every text inside `value`, and the keys of a dictionary of `FREE_MAPS`: `--rename
        delivery=x` must not turn the field `delivery_id` into `x_id`."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                name = distinct(self.text(str(key)), result) if free_keys else str(key)
                result[name] = self.value(item, free_keys=str(key) in FREE_MAPS)
            return result
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


def price_note(input_price: float, output_price: float, note: str | None) -> str:
    """The note given, or one that says where the rates come from."""
    if note is not None:
        return note
    if (input_price, output_price) == (DEFAULT_INPUT_PRICE, DEFAULT_OUTPUT_PRICE):
        return DEFAULT_PRICE_NOTE
    return GIVEN_PRICE_NOTE


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
    try:
        db = connect_read_only(path)
        try:
            rows = db.execute(_MESSAGES).fetchall()
        finally:
            db.close()
    except sqlite3.Error as error:
        raise ReplayError(f"--transcript {path}: cannot read it: {error}") from None
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
    """Check every exported record against its hash and the link to the one before it.

    This is all the check covers: neither the head the hub keeps of the stream nor its anchor
    outside the store is read, and the chain has no key. A record cut after the last exported
    one, or a log rewritten whole with its hashes recomputed, goes unnoticed here; `log.verify`
    on the hub is the check for those.
    """
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
    """One contract object in the shape the format names; a field an object lacks is null.

    `verification` is the object's own when it has one (a criterion: an attack identifier or a
    method letter), else its methods; `test` is the acceptance test a criterion names.
    """
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
        "test": item.get("test"),
    }


def contract_objects(objects: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every object of a version, in the order of its identifier."""
    return [
        contract_object(str(key), item)
        for key, item in sorted(objects.items())
        if isinstance(item, Mapping)
    ]


def contract_document(version: Mapping[str, Any] | None) -> dict[str, Any]:
    """A version as the document's `contract` names it; no version is an empty contract."""
    if version is None:
        return {"version": None, "version_id": None, "objects": []}
    objects = version["objects"]
    return {
        "version": version["number"],
        "version_id": version["version_id"],
        "objects": contract_objects(objects if isinstance(objects, Mapping) else {}),
    }


def contract_at(
    versions: Sequence[Mapping[str, Any]], moment: datetime | None, *, before: bool
) -> dict[str, Any]:
    """The contract in force at `moment`: the last version written before it (`before`), or at
    the latest at it. With no moment, the last version."""
    chosen: Mapping[str, Any] | None = None
    for version in versions:
        written = parse_moment(str(version["time"]))
        if moment is None or written < moment or (not before and written == moment):
            chosen = version
    return contract_document(chosen)


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
    hub_state: dict[str, Any] | None = None
    """For a `hub-log` event: what the hub's tables add to its record, as it stood then."""

    def render(self, identifier: int, started: datetime) -> dict[str, Any]:
        rendered: dict[str, Any] = {
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
        if self.hub_state is not None:
            rendered["hub_state"] = self.hub_state
        return rendered


Summary = Callable[[str, str, dict[str, Any]], str]
"""What one record says, in one line, from its actor, its outcome and its data. A summary may
add to the data what it derives from the record alone (the number of a version)."""


class _Fields(dict[str, Any]):
    """The fields a template names; one the record lacks reads `?`."""

    def __missing__(self, key: str) -> str:
        return "?"


def _says(template: str) -> Summary:
    """A summary that fills `template` from the record's data and `{actor}`, its label."""

    def summary(actor: str, _outcome: str, data: dict[str, Any]) -> str:
        return template.format_map(_Fields(data, actor=label(actor)))

    return summary


def _failure(data: Mapping[str, Any]) -> str:
    return str(data.get("error") or data.get("detail") or "no detail logged")


def _refused(action: str, reason: str, data: Mapping[str, Any]) -> str:
    if data.get("detail"):
        what = str(data["detail"])
    elif action == "delivery.check":
        files = plural(data.get("paths", "?"), "acceptance test file")
        what = f"delivery {data.get('delivery_id', '?')} changes {files}"
    elif action == "egress.refused":
        what = f"{data.get('purpose', 'a connection')} to {data.get('host', '?')}"
    else:
        what = action
    return f"Refused ({reason}): {what}"


def _contract_read(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    number = version_number(data.get("version_id"))
    if number is not None:
        data["version"] = number
    named = number if number is not None else data.get("version_id", "?")
    return f"{label(actor)} reads contract version {named}"


def _contract_propose(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    operations = plural(data.get("operations", "?"), "operation")
    return (
        f"{label(actor)} proposes change {data.get('change_id', '?')} ({operations}): "
        f"{data.get('summary') or ''}"
    )


def _contract_approve(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    number = version_number(data.get("version_id"))
    named = f"version {number}" if number is not None else data.get("version_id", "?")
    return f"{label(actor)} approves change {data.get('change_id', '?')}: contract {named}"


def _contract_import(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    number = version_number(data.get("version_id"))
    named = f"version {number}" if number is not None else data.get("version_id", "?")
    objects = plural(data.get("objects", "?"), "object")
    return f"{label(actor)} imports the contract as {named} ({objects})"


def _contract_apply_reserved(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    number = version_number(data.get("version_id"))
    named = f"version {number}" if number is not None else data.get("version_id", "?")
    return f"{label(actor)} applies a reserved change as {named}: {data.get('summary') or ''}"


def _turn_side(actor: str, data: Mapping[str, Any]) -> str:
    return label(str(data.get("side") or actor))


def _instance_start(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    chars = plural(data.get("prompt_chars", "?"), "prompt char")
    return f"{label(actor)} starts the {_turn_side(actor, data)} instance ({chars})"


def _instance_stop(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    return f"{label(actor)} stops the {_turn_side(actor, data)} instance"


def _turn_start(actor: str, outcome: str, data: dict[str, Any]) -> str:
    if outcome == OUTCOME_ERROR:
        return f"{_turn_side(actor, data)} turn cannot start: {_failure(data)}"
    chars = plural(data.get("prompt_chars", "?"), "prompt char")
    return f"{_turn_side(actor, data)} turn begins ({chars})"


def _turn_end(actor: str, outcome: str, data: dict[str, Any]) -> str:
    ends = "ends in error" if outcome == OUTCOME_ERROR else "ends"
    return f"{_turn_side(actor, data)} turn {ends}: {data.get('result', '?')}"


def _delivery_push(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    return (
        f"{label(actor)} pushes {data.get('delivery_id', '?')} on {data.get('branch', '?')} "
        f"({plural(data.get('changed', '?'), 'changed path')})"
    )


def _delivery_check(_actor: str, _outcome: str, data: dict[str, Any]) -> str:
    paths = plural(data.get("flagged", "?"), "protected path")
    return f"Hub flags delivery {data.get('delivery_id', '?')}: it changes {paths}"


def _staging_prepare(_actor: str, outcome: str, data: dict[str, Any]) -> str:
    if outcome == OUTCOME_ERROR:
        return f"Hub cannot prepare the staging area: {_failure(data)}"
    return (
        f"Hub prepares the staging area from {data.get('repository', '?')} "
        f"{data.get('branch', '?')} at {data.get('base', '?')}"
    )


def _staging_read(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    what = f"delivery {data['delivery_id']}" if data.get("delivery_id") else "the base"
    size = plural(data.get("bytes", "?"), "byte")
    return f"{label(actor)} reads {what} from the staging area ({size})"


def _project_prepare(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    return (
        f"{label(actor)} asks for a staging area from {data.get('repository', '?')} "
        f"{data.get('branch', '?')}"
    )


def _verification_run(_actor: str, outcome: str, data: dict[str, Any]) -> str:
    delivery = data.get("delivery_id", "?")
    if outcome == OUTCOME_ERROR:
        return f"Verification of {delivery} failed: {_failure(data)}"
    return (
        f"Verification: {data.get('passed', '?')}/{data.get('tests', '?')} acceptance tests "
        f"passed for {delivery}"
    )


def _pull_request(verb: str, failed: str) -> Summary:
    def summary(_actor: str, outcome: str, data: dict[str, Any]) -> str:
        delivery = data.get("delivery_id", "?")
        if outcome == OUTCOME_ERROR:
            return f"Hub cannot {failed} for {delivery}: {_failure(data)}"
        return f"Hub {verb} pull request #{data.get('number', '?')} for {delivery}"

    return summary


def _gate_decision(_actor: str, _outcome: str, data: dict[str, Any]) -> str:
    version = data.get("contract_version")
    suffix = "" if version is None else f" (contract v{version})"
    summary = f"Gate: {data.get('decision', '?')} for {data.get('delivery_id', '?')}{suffix}"
    if data.get("after"):
        summary += f", after an {data['after']}"
        if data.get("previous"):
            summary += f"; it was {data['previous']}"
    return summary


def _inspection_record(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    return (
        f"{label(actor)} records the inspection of {data.get('object_id', '?')} on "
        f"{data.get('delivery_id', '?')}: {data.get('verdict', '?')}"
    )


def _delivery_review(actor: str, _outcome: str, data: dict[str, Any]) -> str:
    return f"{label(actor)} reviews {data.get('delivery_id', '?')}: {data.get('verdict', '?')}"


def _token(verb: str, preposition: str) -> Summary:
    def summary(_actor: str, _outcome: str, data: dict[str, Any]) -> str:
        count = data.get("count")
        tokens = plural(count, "token") if count is not None else "a token"
        why = f" ({data['why']})" if data.get("why") else ""
        return f"Hub {verb} {tokens} {preposition} the {_turn_side('hub', data)} side{why}"

    return summary


def _notified(data: Mapping[str, Any]) -> str:
    """What a notification was about: an event of the delivery loop, or a continuity step."""
    if data.get("event"):
        subject = str(data["event"])
        if data.get("delivery_id"):
            subject += f" for {data['delivery_id']}"
            if data.get("decision"):
                subject += f" ({data['decision']})"
        elif data.get("branch"):
            subject += f" on {data['branch']}"
        return subject
    if data.get("step"):
        return _continuity("hub", OUTCOME_OK, dict(data)).removeprefix("Continuity: ")
    return "an event"


def _notification(_actor: str, outcome: str, data: dict[str, Any]) -> str:
    if outcome == OUTCOME_ERROR:
        return f"Hub cannot notify the operator of {_notified(data)}: {_failure(data)}"
    return f"Hub notifies the operator: {_notified(data)}"


def _continuity(_actor: str, _outcome: str, data: dict[str, Any]) -> str:
    step = str(data.get("step", "?")).replace("_", " ")
    task = data.get("task", "?")
    return f"Continuity: {step}, {_turn_side('hub', data)} side, {task} awaited"


SUMMARIES: dict[str, Summary] = {
    "project.create": _says("{actor} creates project {name}"),
    "project.prepare": _project_prepare,
    "contract.read": _contract_read,
    "contract.propose": _contract_propose,
    "contract.approve": _contract_approve,
    "contract.reject": _says("{actor} rejects change {change_id}"),
    "contract.pending": _says("{actor} reads the pending changes ({pending})"),
    "contract.import": _contract_import,
    "contract.apply_reserved": _contract_apply_reserved,
    "acceptance.put": _says("{actor} stores acceptance test {path}"),
    "instance.start": _instance_start,
    "instance.stop": _instance_stop,
    "turn.start": _turn_start,
    "turn.end": _turn_end,
    "delivery.push": _delivery_push,
    "delivery.declare": _says("{actor} declares delivery {delivery_id}"),
    "delivery.status": _says("{actor} reads the state of {delivery_id}"),
    "delivery.check": _delivery_check,
    "staging.prepare": _staging_prepare,
    "staging.read": _staging_read,
    "verification.run": _verification_run,
    "gate.decision": _gate_decision,
    "pull_request.open": _pull_request("opens", "open a pull request"),
    "pull_request.comment": _pull_request("comments on", "comment on the pull request"),
    "pull_request.retitle": _pull_request("retitles", "retitle the pull request"),
    "inspection.record": _inspection_record,
    "delivery.review": _delivery_review,
    "token.issue": _token("issues", "to"),
    "token.revoke": _token("revokes", "of"),
    "notification.sent": _notification,
    "continuity.step": _continuity,
}


def _model_call(actor: str, outcome: str, data: dict[str, Any], prices: Prices) -> str:
    """A call to the gateway, with its indicative cost when it was answered."""
    data["side"] = actor
    if outcome != OUTCOME_OK:
        return f"Model call failed: {_failure(data)}"
    prompt = data["prompt_tokens"] = int(data.get("prompt_tokens") or 0)
    completion = data["completion_tokens"] = int(data.get("completion_tokens") or 0)
    tool_calls = data["tool_calls"] = int(data.get("tool_calls") or 0)
    data["cost_usd"] = prices.cost(prompt, completion)
    return f"Model call: {prompt:,} in / {completion:,} out, {plural(tool_calls, 'tool call')}"


def hub_log_event(record: LogRecord, prices: Prices) -> Event:
    """One record of the log as one event of the replay: its data is what the record holds."""
    actor = actor_of(record.principal)
    data = kept(record.parameters)
    category = CATEGORY_BY_ACTION.get(record.action, DEFAULT_CATEGORY)
    if record.outcome == OUTCOME_REFUSED:
        category = CATEGORY_REFUSAL
        data.update(reason=record.reason, action=record.action)
        data.setdefault("detail", None)
        summary = _refused(record.action, record.reason, data)
    elif category == "model":
        summary = _model_call(actor, record.outcome, data, prices)
    elif record.action in SUMMARIES:
        summary = SUMMARIES[record.action](actor, record.outcome, data)
    elif record.outcome == OUTCOME_ERROR:
        summary = f"{label(actor)} {record.action} failed: {_failure(data)}"
    else:
        summary = f"{label(actor)} {record.action}"
    return Event(
        when=record.time,
        sequence=record.seq,
        source=SOURCE_LOG,
        actor=actor,
        action=record.action,
        category=category,
        outcome=record.outcome,
        summary=shorten(summary, MAX_SUMMARY),
        data=data,
    )


def _judged(
    stored: object, name: str, key: tuple[str, ...], when: str, placed: set[tuple[str, ...]]
) -> dict[str, Any] | None:
    """The `name` field of a stored inspection or review, for the first record of it at or after
    the time the row gives (the hub dates the row, then logs the record), and for no other."""
    if not isinstance(stored, Mapping) or stored.get(name) is None or key in placed:
        return None
    at = stored.get("at")
    if not isinstance(at, str) or parse_moment(at) > parse_moment(when):
        return None
    placed.add(key)
    return {name: str(stored[name])}


def state_of(
    event: Event, body: Mapping[str, Any], placed: set[tuple[str, ...]]
) -> dict[str, Any] | None:
    """What the delivery's row adds to `event` and held at its time, or None.

    The row keeps the last state of the delivery. What never changes once written holds for
    every event after it: the paths a push changed, the paths the check flagged, the outcomes of
    the verification (an inspection changes the decision, not them), the URL of the pull
    request. An inspection or a review can be recorded again and the row keeps the last one, so
    it belongs to one event only.
    """
    action = event.action
    delivery_id = str(event.data.get("delivery_id") or "")
    if action == "delivery.push" and body.get("changed_paths"):
        return {"changed_paths": [str(path) for path in body["changed_paths"]]}
    if action == "delivery.check" and body.get("flagged_paths"):
        return {"paths": [str(path) for path in body["flagged_paths"]]}
    if action == "gate.decision" and body.get("outcomes"):
        return {"outcomes": dict(body["outcomes"])}
    if action in PULL_REQUEST_ACTIONS and body.get("pull_request"):
        return {"url": str(body["pull_request"])}
    if action == "inspection.record":
        object_id = str(event.data.get("object_id") or "")
        stored = (body.get("inspections") or {}).get(object_id)
        return _judged(stored, "observation", (action, delivery_id, object_id), event.when, placed)
    if action == "delivery.review":
        return _judged(body.get("review"), "reasons", (action, delivery_id), event.when, placed)
    return None


def complete_from_state(
    events: Sequence[Event], deliveries: Mapping[str, Mapping[str, Any]]
) -> None:
    """Give each accepted `hub-log` event of a delivery its `hub_state`, in the order of the log."""
    placed: set[tuple[str, ...]] = set()
    for event in events:
        body = deliveries.get(str(event.data.get("delivery_id") or ""))
        if event.source != SOURCE_LOG or event.outcome != OUTCOME_OK or not body:
            continue
        found = state_of(event, body, placed)
        if found is not None:
            event.hub_state = found


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
    """The protected paths a delivery changed when the hub did not flag such paths yet.

    A hub that flags them records a `delivery.check` when it verifies the delivery, and keeps
    the paths in the delivery's row: that record is the flag, and this function adds nothing. A
    run older than the rule carries neither, and the exporter finds the paths among the
    delivered ones, at the time the delivery was received (`by: "exporter"`).
    """
    received = body.get("received_at")
    if received is None or body.get("flagged_paths"):
        return None
    changed = tuple(str(path) for path in body.get("changed_paths") or ())
    paths = list(flagged_paths(changed, tuple(protected)))
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
        data={
            "delivery_id": delivery_id,
            "paths": paths,
            "rule": "protected-path",
            "by": "exporter",
        },
    )


def transcript_events(
    side: str,
    messages: Sequence[tuple[str, str, str, float]],
    window: tuple[datetime, datetime] | None,
    message_chars: int,
) -> list[Event]:
    """The answers of one instance: an assistant message that says something, inside the run.

    A state database can hold the same message in more than one session (a real run did): a
    message is kept once, by its role, its content and its time, as it reads in the first session
    that holds it.
    """
    if window is None:
        return []
    by_session: dict[str, list[tuple[str, str, str, float]]] = {}
    for session, role, text, timestamp in messages:
        by_session.setdefault(session, []).append((session, role, text, timestamp))
    events: list[Event] = []
    kept_messages: set[tuple[str, str, float]] = set()
    for session in sorted(by_session):
        conversation = by_session[session]
        for index, (_, role, text, timestamp) in enumerate(conversation):
            if role != "assistant" or not text.strip() or (role, text, timestamp) in kept_messages:
                continue
            kept_messages.add((role, text, timestamp))
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
    """The sums of the run. A model call counts once it was answered, as the hub counts it in
    its evidence report; a refusal counts for whoever was refused, the hub included."""
    model_calls: dict[str, int] = {}
    tokens: dict[str, dict[str, int]] = {}
    cost_usd: dict[str, float] = {}
    refusals: dict[str, int] = {}
    decisions: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    for event in events:
        if event.category == "model" and event.outcome == OUTCOME_OK:
            side = str(event.data.get("side") or event.actor)
            model_calls[side] = model_calls.get(side, 0) + 1
            counted = tokens.setdefault(side, {"input": 0, "output": 0})
            counted["input"] += int(event.data.get("prompt_tokens") or 0)
            counted["output"] += int(event.data.get("completion_tokens") or 0)
            cost = float(event.data.get("cost_usd") or 0)
            cost_usd[side] = round(cost_usd.get(side, 0.0) + cost, 6)
        elif event.category == CATEGORY_REFUSAL:
            refusals[event.actor] = refusals.get(event.actor, 0) + 1
        if event.action == "gate.decision" and event.outcome == OUTCOME_OK:
            decisions.append(
                {
                    "delivery_id": event.data.get("delivery_id"),
                    "decision": event.data.get("decision"),
                    "t": event.when,
                }
            )
        elif (
            event.action == "delivery.review"
            and event.outcome == OUTCOME_OK
            and event.data.get("verdict")
        ):
            verdicts.append(
                {
                    "delivery_id": event.data.get("delivery_id"),
                    "verdict": event.data.get("verdict"),
                    "t": event.when,
                }
            )
    sides = sorted(model_calls)
    return {
        "events": len(events),
        "model_calls": {side: model_calls[side] for side in sides},
        "tokens": {side: tokens[side] for side in sides},
        "cost_usd": {
            **{side: cost_usd[side] for side in sides},
            "total": round(float(sum(cost_usd.values())), 6),
        },
        "refusals": dict(sorted(refusals.items())),
        "decisions": decisions,
        "verdicts": verdicts,
        "duration_ms": round((ended - started).total_seconds() * 1000),
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
    price_note: str | None = None


def replay_document(options: Options, exported_at: str | None = None) -> dict[str, Any]:
    """Build the whole replay document for one project and one range of its log."""
    note = price_note(options.input_price, options.output_price, options.price_note)
    prices = Prices(options.input_price, options.output_price, note)
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
        window = (
            (parse_moment(records[0].time), parse_moment(records[-1].time)) if records else None
        )
        events = [hub_log_event(record, prices) for record in records]
        complete_from_state(events, deliveries)
        named_versions = {
            str(record.parameters["version_id"])
            for record in records
            if record.action in VERSION_ACTIONS
            and record.outcome == OUTCOME_OK
            and record.parameters.get("version_id")
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
        "contract_at_start": redactor.value(contract_at(versions, started, before=True))
        if started is not None
        else contract_document(None),
        "contract": redactor.value(contract_at(versions, ended, before=False)),
        "events": [event.render(index, started) for index, event in enumerate(events)]
        if started is not None
        else [],
        "totals": totals_of(events, started, ended)
        if started is not None and ended is not None
        else empty_totals(),
    }


def empty_totals() -> dict[str, Any]:
    return {
        "events": 0,
        "model_calls": {},
        "tokens": {},
        "cost_usd": {"total": 0.0},
        "refusals": {},
        "decisions": [],
        "verdicts": [],
        "duration_ms": 0,
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
    if event.hub_state is not None:
        event.hub_state = redactor.value(event.hub_state)
    return event


def document_text(document: Mapping[str, Any]) -> str:
    """The deterministic JSON text of a replay."""
    return json.dumps(document, indent=1, ensure_ascii=False, sort_keys=False)


# ----------------------------------------------------------------------------- the player
def player_document(template: str, payload: str) -> str:
    """The player with the replay inside it.

    Every `<` of the JSON is written `\\u003c`, which JSON reads back as `<`: no text of the
    replay can then close the element, open a comment or start a script inside the page.
    """
    if template.count(PLAYER_DATA) != 1:
        raise ReplayError(f"the player must hold exactly one {PLAYER_DATA!r} element")
    escaped = payload.replace("<", "\\u003c")
    inner = f'<script type="application/json" id="replay-data">{escaped}</script>'
    return template.replace(PLAYER_DATA, inner)


def write_player(path: Path, payload: str) -> None:
    if not PLAYER_TEMPLATE.is_file():
        raise ReplayError(f"the player template is missing: {PLAYER_TEMPLATE}")
    template = PLAYER_TEMPLATE.read_text(encoding="utf-8")
    write_text(path, player_document(template, payload))


def write_text(path: Path, text: str) -> None:
    """Write a file of the exporter; a failure names the file."""
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        raise ReplayError(f"cannot write {path}: {error.strerror or error}") from None


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
    parser.add_argument("--kind", choices=KINDS, default="real")
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
    parser.add_argument("--price-note", dest="price_note", default=None)
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
            write_text(arguments.output, text + "\n")
    except ReplayError as error:
        print(f"replay export: {error}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"replay export: cannot read {arguments.db}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
