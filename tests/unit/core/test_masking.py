"""EXG-49, ATK-84: no canary reaches the log, the evidence report or a pull request body.

The four canaries of the attack catalog: a model-provider key pattern, a git-hosting token, a
project token, and a private-key header. Planted in a message, in a log line, in test output
and in a delivered file, each must come back as a masked value.
"""

from __future__ import annotations

from zephyros.core.audit import canonical_parameters
from zephyros.core.masking import MASK, mask_text, mask_value
from zephyros.core.report import mask as mask_report

CANARIES = [
    "sk-live-abcdefghijklmnop1234",  # pragma: allowlist secret (a canary of ATK-84)
    "ghp_abcdefghijklmnopqrstuvwxyz012345",  # pragma: allowlist secret (a canary of ATK-84)
    "zph_t_abcdefghijklmnopqrstuvwx",  # pragma: allowlist secret (a canary of ATK-84)
    "-----BEGIN OPENSSH PRIVATE KEY-----",  # pragma: allowlist secret (a canary of ATK-84)
]
KNOWN = "correct-horse-battery-staple"  # a secret of no recognisable shape


def test_every_canary_shape_is_masked() -> None:
    for canary in CANARIES:
        assert canary not in mask_text(f"before {canary} after")
        assert MASK in mask_text(f"before {canary} after")


def test_a_known_secret_without_a_shape_is_masked_by_name() -> None:
    assert mask_text(f"token={KNOWN}", [KNOWN]) == f"token={MASK}"
    assert mask_text(f"token={KNOWN}") == f"token={KNOWN}"  # unknown, unseen: nothing to do


def test_a_message_planted_in_the_log_comes_back_masked() -> None:
    parameters = {
        "summary": "see " + CANARIES[1],
        "operations": [{"path": "notes.md", "content": CANARIES[3]}],
    }
    text = canonical_parameters(parameters)
    for canary in CANARIES:
        assert canary not in text
    assert MASK in text


def test_the_evidence_report_and_the_pull_request_body_hold_no_canary() -> None:
    body = "test output: " + " ".join(CANARIES) + f" and {KNOWN}"
    masked = mask_report(body, [KNOWN])
    for canary in CANARIES:
        assert canary not in masked
    assert KNOWN not in masked
    assert masked.count(MASK) == len(CANARIES) + 1


def test_a_structure_is_masked_everywhere() -> None:
    value = {"log": [CANARIES[0], {"nested": CANARIES[2]}], CANARIES[1]: "key too"}
    masked = mask_value(value)
    assert CANARIES[1] not in str(masked)  # the key as well
    assert CANARIES[0] not in str(masked)
