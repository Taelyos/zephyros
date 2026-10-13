"""EXG-49, ATK-84: no canary reaches the log, the evidence report or a pull request body.

The four canaries of the attack catalog: a model-provider key pattern, a git-hosting token, a
project token, and a private-key header. Planted in a message, in a log line, in test output
and in a delivered file, each must come back as a masked value.
"""

from __future__ import annotations

import json

from tests.unit.core.test_gate import contract
from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.core.audit import (
    Action,
    AuditEvent,
    AuditLog,
    Outcome,
    canonical_parameters,
    format_line,
)
from zephyros.core.gate import compute_gate
from zephyros.core.masking import MASK, mask_text, mask_value
from zephyros.core.ports.delivery import Delivery
from zephyros.core.ports.verification import Outcome as RunOutcome
from zephyros.core.report import Cost, ReportInput, render
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


# ----------------------------------------------------------------------------- no false positive
# Words that end with a key prefix ("risk-", "disk-", "task-" end with "sk-") are not keys: a
# shape counts only where a token starts, never inside a word.
ORDINARY = [
    "risk-management-module",
    "src/disk-usage-monitoring.py",
    "task-scheduler-configuration",
    "flask-sqlalchemy-integration",
    "kiosk-mode-settings-panel",
    "ask-before-merging-anything",
]


def test_ordinary_words_that_hold_a_key_prefix_stay_intact() -> None:
    for text in ORDINARY:
        assert mask_text(f"see {text} here") == f"see {text} here"
        assert mask_text(text) == text


def test_a_canary_after_a_separator_is_still_masked() -> None:
    """A separator ends a word: a canary glued to one is masked, as in the branch name
    `add-sub-<canary>` that the runtime walk plants."""
    for canary in CANARIES:
        for before in ("add-sub-", "x_", "path/", "key=", "token:", '"', "(", "\n", ""):
            masked = mask_text(f"{before}{canary}.")
            assert canary not in masked, (before, canary)
            assert masked == f"{before}{MASK}.", (before, canary)


def test_a_known_secret_is_masked_even_inside_a_word() -> None:
    """The secrets the hub knows of are replaced by value, wherever they stand."""
    assert mask_text(f"x{KNOWN}y", [KNOWN]) == f"x{MASK}y"


def test_ordinary_words_reach_the_log_intact() -> None:
    log = AuditLog(InMemoryLogStore(), FakeClock())
    record = log.append(
        AuditEvent(
            "p_demo",
            "contractor:p_demo",
            Action.DELIVERY_PUSH,
            Outcome.OK,
            parameters={"branch": ORDINARY[0], "paths": ORDINARY[1:]},
        )
    )
    assert json.loads(record.parameters) == {"branch": ORDINARY[0], "paths": ORDINARY[1:]}
    assert MASK not in format_line(record)


def test_ordinary_words_reach_the_evidence_report_intact() -> None:
    objects = contract((1, "P0", ("test",), ("src/disk-usage-monitoring.py::test_usage",)))
    outcomes = {"src/disk-usage-monitoring.py::test_usage": RunOutcome.PASSED}
    delivery = Delivery("p_x", "d1", "risk-management-module", "a" * 40, "b" * 40, tuple(ORDINARY))
    text = render(
        ReportInput(
            project_name="Monitoring",
            delivery=delivery,
            contract_version=1,
            contract_hash="c" * 64,
            kit="tests",
            gate=compute_gate(objects, outcomes),
            outcomes=outcomes,
            log="\n".join(f"checked {text}" for text in ORDINARY),
            cost=Cost(),
            produced_at="2026-10-10T12:00:00Z",
            produced_by="Zephyros 0.0.0",
        )
    )
    assert MASK not in text
    for ordinary in ORDINARY:
        assert f"checked {ordinary}" in text
        assert f"`{ordinary}`" in text  # a changed path, shown as code


def test_a_canary_planted_as_a_key_of_the_parameters_is_masked() -> None:
    """The log masks every string it keeps, the keys of the parameters as well as the values."""
    text = canonical_parameters({CANARIES[1]: "value", "nested": {CANARIES[0]: 1}})
    for canary in CANARIES[:2]:
        assert canary not in text
    assert text.count(MASK) == 2
