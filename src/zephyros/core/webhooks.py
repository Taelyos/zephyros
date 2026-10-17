"""Verifiable webhooks (EXG-43): the hub signs what it sends, the receiver checks before acting.

Every delivery carries three headers: `X-Zephyros-Delivery` (an identifier), `X-Zephyros-Timestamp`
(Unix seconds) and `X-Zephyros-Signature` (HMAC-SHA256, hexadecimal, over `id.timestamp.body`
under the shared secret). A receiver calls `verify`: it accepts only a fresh delivery whose body
it signed, whose identifier it has not seen; a missing, wrong or old signature, a modified body,
a repeated identifier and a stale timestamp are refused (ATK-78) and logged.
"""

from __future__ import annotations

import hmac
from collections.abc import Collection
from dataclasses import dataclass
from hashlib import sha256

SIGNATURE_HEADER = "X-Zephyros-Signature"
DELIVERY_HEADER = "X-Zephyros-Delivery"
TIMESTAMP_HEADER = "X-Zephyros-Timestamp"

TOLERANCE_SECONDS = 300
"""How far the timestamp of a delivery may be from the receiver's clock."""

EVENTS_SCHEMA_VERSION = "2.2.0"
"""The version of the event payloads, part of the public API: every notification carries it.
2.0.0: `delivery.decided` can say `REFUSED` or `ERROR`, and an alert of the continuity ladder
can carry `side: main-agent`; a receiver that checked a closed list of values breaks.
2.1.0: the event `obstacle.raised` (ADR 0034), and the tasks `obstacle` and `obstacle_raised`
in the alerts of the continuity ladder.
2.2.0: the event `instance.token_refused` (ADR 0016, amended)."""


def sign(secret: str | bytes, delivery_id: str, timestamp: int, body: bytes) -> str:
    """The signature of one delivery: HMAC-SHA256 of `id.timestamp.body`, hexadecimal."""
    key = secret.encode() if isinstance(secret, str) else secret
    message = f"{delivery_id}.{timestamp}.".encode() + body
    return hmac.new(key, message, sha256).hexdigest()


def verify(  # reason: the five items of a delivery, and the receiver's state
    secret: str | bytes,
    *,
    delivery_id: str | None,
    timestamp: int | None,
    body: bytes,
    signature: str | None,
    seen: Collection[str],
    now: int,
    tolerance: int = TOLERANCE_SECONDS,
) -> str | None:
    """`None` when the delivery is accepted; the reason of the refusal otherwise.

    The reasons: `missing` (a header is absent), `bad_signature` (for a wrong signature and for a
    modified body), `stale_timestamp`, `replayed` (the identifier was seen). The receiver adds
    the identifier to `seen` once it accepts, and logs every refusal.
    """
    if not delivery_id or timestamp is None or not signature:
        return "missing"
    if not hmac.compare_digest(sign(secret, delivery_id, timestamp, body), signature):
        return "bad_signature"
    if abs(now - timestamp) > tolerance:
        return "stale_timestamp"
    if delivery_id in seen:
        return "replayed"
    return None


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """What came of one notification: the status the receiver answered, and the host the hub
    refused to reach, if any (EXG-48). `refused` names the host; `status` is the answer when a
    request was sent."""

    status: int | None = None
    refused: str = ""
