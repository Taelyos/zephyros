"""The audit log of the hub (EXG-19; US-E2-03, US-E2-05).

The log is an ordered sequence of records per stream, a stream being a project or the hub
itself. Each record is hashed over its canonical form and the hash of the previous record, so
that a verification tool finds the first altered record. A separate head (the sequence number
and hash of the last record) is kept apart from the records, so that a truncation is found.

What the log never holds is free text from an agent in a field that names something. The
action is one of a closed set, the reason is one of a closed set, and the principal is an
identifier that the hub issued. Whatever an agent supplies goes in `parameters`, as JSON
values that the serializer escapes, so that no value can start a new line or imitate a record
(ATK-70). The log offers one write operation, append, and no operation to change or remove a
record (ATK-68).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from zephyros.core.clock import Clock, format_time
from zephyros.core.masking import mask_text

GENESIS_HASH = "0" * 64
HUB_STREAM = "hub"
MAX_STRING = 512
MAX_PARAMETERS_BYTES = 4096
MAX_DEPTH = 4

_STREAM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PRINCIPAL = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class Outcome(StrEnum):
    OK = "ok"
    REFUSED = "refused"
    ERROR = "error"


class Action(StrEnum):
    """The closed set of things the hub records. Anything else is `unknown`, and the name an
    agent used goes in the parameters."""

    UNKNOWN = "unknown"
    WHOAMI = "whoami"
    PROJECT_CREATE = "project.create"
    PROJECT_LIST = "project.list"
    PROJECT_GET = "project.get"
    INSTANCE_START = "instance.start"
    INSTANCE_STOP = "instance.stop"
    INSTANCE_STATUS = "instance.status"
    TURN_START = "turn.start"
    TURN_END = "turn.end"
    CONTRACT_READ = "contract.read"
    CONTRACT_PROPOSE = "contract.propose"
    CONTRACT_APPROVE = "contract.approve"
    CONTRACT_REJECT = "contract.reject"
    CONTRACT_APPLY_RESERVED = "contract.apply_reserved"
    CONTRACT_IMPORT = "contract.import"
    CONTRACT_PENDING = "contract.pending"
    CONTINUITY = "continuity.step"
    NOTIFICATION = "notification.sent"
    EGRESS = "egress.refused"
    TOKEN_ISSUE = "token.issue"  # noqa: S105  # reason: an action name, not a secret
    TOKEN_REVOKE = "token.revoke"  # noqa: S105  # reason: an action name, not a secret
    GATEWAY_CALL = "gateway.call"
    LOG_READ = "log.read"
    LOG_VERIFY = "log.verify"
    OPERATOR_CHANNEL = "operator.channel"
    PROJECT_PREPARE = "project.prepare"
    ACCEPTANCE_PUT = "acceptance.put"
    STAGING_READ = "staging.read"
    DELIVERY_PUSH = "delivery.push"
    DELIVERY_DECLARE = "delivery.declare"
    DELIVERY_STATUS = "delivery.status"
    STAGING_PREPARE = "staging.prepare"
    DELIVERY_CHECK = "delivery.check"
    VERIFICATION_RUN = "verification.run"
    GATE_DECISION = "gate.decision"
    PULL_REQUEST_OPEN = "pull_request.open"
    AUTHENTICATION = "authentication"


class Reason(StrEnum):
    """Why an action was refused, as a stable code."""

    NONE = "none"
    UNAUTHENTICATED = "unauthenticated"
    UNKNOWN_TOKEN = "unknown_token"  # noqa: S105  # reason: a reason code, not a secret
    WRONG_SOURCE = "wrong_source"
    EXPIRED = "expired"
    REVOKED = "revoked"
    WRONG_ENDPOINT = "wrong_endpoint"
    FORBIDDEN = "forbidden"
    OTHER_PROJECT = "other_project"
    UNKNOWN_ACTION = "unknown_action"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    SELF_APPROVAL = "self_approval"
    RESERVED_DATA = "reserved_data"
    NOT_PENDING = "not_pending"
    STALE_BASE = "stale_base"
    BROWSER_REQUEST = "browser_request"
    BAD_HOST = "bad_host"
    UNREADABLE = "unreadable"
    BUSY = "busy"
    PROTECTED_PATH = "protected_path"
    EGRESS = "egress"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """What a caller asks the log to record."""

    stream: str
    principal: str
    action: Action
    outcome: Outcome
    reason: Reason = Reason.NONE
    parameters: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not _STREAM.fullmatch(self.stream):
            raise ValueError("a stream is a project identifier or 'hub'")
        if not _PRINCIPAL.fullmatch(self.principal):
            raise ValueError("a principal is an identifier issued by the hub")
        if self.outcome is Outcome.REFUSED and self.reason is Reason.NONE:
            raise ValueError("a refusal states its reason")
        if self.outcome is not Outcome.REFUSED and self.reason is not Reason.NONE:
            raise ValueError("only a refusal states a reason")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    seq: int
    time: str
    stream: str
    principal: str
    action: Action
    outcome: Outcome
    reason: Reason
    parameters: str
    """The canonical JSON text of the parameters, kept as text so the hash is exact."""
    previous: str
    hash: str


@dataclass(frozen=True, slots=True)
class Head:
    """The last record of a stream, kept apart from the records."""

    stream: str
    seq: int
    hash: str


class LogStore(Protocol):
    """Storage of the log. It offers append and reads, and nothing that changes a record."""

    def append(self, record: AuditRecord) -> None:
        """Add a record. The sequence number must follow the last one of the stream."""
        ...

    def last(self, stream: str) -> AuditRecord | None: ...

    def read(self, stream: str, after: int = 0, limit: int = 1000) -> list[AuditRecord]:
        """Records of the stream with a sequence number above `after`, in order."""
        ...

    def head(self, stream: str) -> Head | None: ...

    def set_head(self, head: Head) -> None: ...

    def anchor(self, stream: str) -> Head | None:
        """The head kept outside the store, when the store keeps one (EXG-19, ATK-69): a
        replacement of the whole log cannot move it."""
        ...

    def streams(self) -> list[str]: ...


class LogError(Exception):
    """The log cannot take the record (a store fault, a broken sequence)."""


# ----------------------------------------------------------------------------- canonical form
def _clean(value: object, depth: int) -> object:
    """A JSON value with bounded strings, depth and kinds. Nothing here changes meaning
    except what is cut to its bound; the cut is marked."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        masked: str = mask_text(value)  # EXG-49: the log carries no secret, whatever its shape
        if len(masked) > MAX_STRING:
            return masked[:MAX_STRING] + f"...[{len(masked) - MAX_STRING} more characters]"
        return masked
    if depth >= MAX_DEPTH:
        return "[too deep]"
    if isinstance(value, Mapping):
        return {str(key)[:MAX_STRING]: _clean(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(item, depth + 1) for item in value]
    return f"[{type(value).__name__}]"


def canonical_json(value: object) -> str:
    """Deterministic JSON on one line, ASCII only: every control character, line separator
    and non-ASCII character is escaped."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_parameters(parameters: Mapping[str, object] | None) -> str:
    cleaned = _clean(dict(parameters or {}), 0)
    text = canonical_json(cleaned)
    if len(text.encode("ascii")) > MAX_PARAMETERS_BYTES:
        return canonical_json({"truncated": True, "size": len(text)})
    return text


def compute_hash(
    *,
    seq: int,
    time: str,
    stream: str,
    principal: str,
    action: str,
    outcome: str,
    reason: str,
    parameters: str,
    previous: str,
) -> str:
    body = canonical_json(
        {
            "seq": seq,
            "time": time,
            "stream": stream,
            "principal": principal,
            "action": action,
            "outcome": outcome,
            "reason": reason,
            "parameters": parameters,
            "previous": previous,
        }
    )
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def record_hash(record: AuditRecord) -> str:
    """The hash that `record` should carry, from its other fields."""
    return compute_hash(
        seq=record.seq,
        time=record.time,
        stream=record.stream,
        principal=record.principal,
        action=record.action.value,
        outcome=record.outcome.value,
        reason=record.reason.value,
        parameters=record.parameters,
        previous=record.previous,
    )


def format_line(record: AuditRecord) -> str:
    """The record as one line of text. Every field is escaped, so there is one line per record
    whatever an agent supplied."""
    return canonical_json(
        {
            "seq": record.seq,
            "time": record.time,
            "stream": record.stream,
            "principal": record.principal,
            "action": record.action.value,
            "outcome": record.outcome.value,
            "reason": record.reason.value,
            "parameters": json.loads(record.parameters),
            "previous": record.previous,
            "hash": record.hash,
        }
    )


# ----------------------------------------------------------------------------- the service
@dataclass(frozen=True, slots=True)
class VerifyReport:
    stream: str
    records: int
    ok: bool
    first_bad_seq: int | None
    problem: str | None


class AuditLog:
    """Appends records to a store and verifies them."""

    def __init__(self, store: LogStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> AuditRecord:
        with self._lock:
            last = self._store.last(event.stream)
            seq = 1 if last is None else last.seq + 1
            previous = GENESIS_HASH if last is None else last.hash
            time = format_time(self._clock.now())
            parameters = canonical_parameters(event.parameters)
            digest = compute_hash(
                seq=seq,
                time=time,
                stream=event.stream,
                principal=event.principal,
                action=event.action.value,
                outcome=event.outcome.value,
                reason=event.reason.value,
                parameters=parameters,
                previous=previous,
            )
            record = AuditRecord(
                seq=seq,
                time=time,
                stream=event.stream,
                principal=event.principal,
                action=event.action,
                outcome=event.outcome,
                reason=event.reason,
                parameters=parameters,
                previous=previous,
                hash=digest,
            )
            self._store.append(record)
            self._store.set_head(Head(event.stream, seq, digest))
            return record

    def read(self, stream: str, after: int = 0, limit: int = 1000) -> list[AuditRecord]:
        return self._store.read(stream, after, limit)

    def streams(self) -> list[str]:
        return self._store.streams()

    def verify(self, stream: str) -> VerifyReport:
        """Check every record of `stream`: its hash, its link to the one before, the sequence,
        and the head. Report the first bad record."""
        expected_seq = 1
        previous = GENESIS_HASH
        count = 0
        cursor = 0
        last: AuditRecord | None = None
        while True:
            batch = self._store.read(stream, cursor, 500)
            if not batch:
                break
            for record in batch:
                count += 1
                if record.seq != expected_seq:
                    return VerifyReport(
                        stream, count, False, expected_seq, "the sequence is broken"
                    )
                if record.stream != stream:
                    return VerifyReport(
                        stream, count, False, record.seq, "a record of another stream"
                    )
                if record.previous != previous:
                    return VerifyReport(
                        stream,
                        count,
                        False,
                        record.seq,
                        "the link to the previous record is broken",
                    )
                if record_hash(record) != record.hash:
                    return VerifyReport(
                        stream, count, False, record.seq, "the record does not match its hash"
                    )
                previous = record.hash
                expected_seq += 1
                last = record
                cursor = record.seq
        head = self._store.head(stream)
        if last is None:
            if head is not None and head.seq > 0:
                return VerifyReport(
                    stream, 0, False, 1, "the log is empty but its head names records"
                )
            return VerifyReport(stream, 0, True, None, None)
        if head is None or head.seq != last.seq or head.hash != last.hash:
            return VerifyReport(
                stream, count, False, last.seq, "the last record does not match the head"
            )
        anchor = self._store.anchor(stream)
        if anchor is not None and (anchor.seq != last.seq or anchor.hash != last.hash):
            # EXG-19, ATK-69: the whole log, head included, was replaced or truncated — the
            # anchor kept outside the store still names the records it had.
            return VerifyReport(
                stream,
                count,
                False,
                min(anchor.seq, last.seq) or 1,
                "the log does not match the anchor kept outside the store",
            )
        return VerifyReport(stream, count, True, None, None)
