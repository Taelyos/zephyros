"""Verifiable webhooks (EXG-43): signatures, freshness, replay (ATK-78)."""

from __future__ import annotations

from zephyros.core.webhooks import TOLERANCE_SECONDS, sign, verify

SECRET = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
OTHER_SECRET = "fedcba9876543210fedcba9876543210"  # pragma: allowlist secret
NOW = 1_800_000_000
BODY = b'{"event":"delivery.decided","delivery":"d_0001"}'


def check(**overrides: object) -> str | None:
    items: dict[str, object] = {
        "delivery_id": "w_0001",
        "timestamp": NOW,
        "body": BODY,
        "signature": sign(SECRET, "w_0001", NOW, BODY),
        "seen": (),
        "now": NOW,
    }
    items.update(overrides)
    return verify(SECRET, **items)  # type: ignore[arg-type]  # reason: the defaults are well typed


def test_a_fresh_signed_delivery_is_accepted() -> None:
    assert check() is None


def test_a_missing_header_is_refused() -> None:
    assert check(signature=None) == "missing"
    assert check(delivery_id=None) == "missing"
    assert check(timestamp=None) == "missing"


def test_a_wrong_signature_is_refused() -> None:
    assert check(signature="0" * 64) == "bad_signature"
    assert check(signature=sign(OTHER_SECRET, "w_0001", NOW, BODY)) == "bad_signature"


def test_a_modified_body_is_refused() -> None:
    assert check(body=b'{"event":"delivery.decided","delivery":"d_9999"}') == "bad_signature"


def test_an_old_signature_is_refused() -> None:
    old = NOW - TOLERANCE_SECONDS - 1
    assert check(timestamp=old, signature=sign(SECRET, "w_0001", old, BODY)) == "stale_timestamp"


def test_the_tolerance_edge_is_still_fresh() -> None:
    edge = NOW - TOLERANCE_SECONDS
    assert check(timestamp=edge, signature=sign(SECRET, "w_0001", edge, BODY)) is None


def test_a_repeated_identifier_is_refused() -> None:
    assert check(seen={"w_0001"}) == "replayed"
