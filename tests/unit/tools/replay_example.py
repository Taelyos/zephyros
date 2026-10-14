"""The example replay the repository ships (`tools/replay/example.replay.json`), and how it is made.

The example is synthetic: no run happened and no model answered. Test code writes a small run into
a hub database with the hub's own log and stores — the records the hub writes, from the principals
that write them, with the parameters they carry — and the exporter turns it into a replay as it
would a real run. Its contract passes the hub's checks and its decisions are the gate's own. A test
compares the file with a new export, so the file is always what the exporter produces.

The run: the operator imports a contract; the Owner writes an acceptance test and proposes a
criterion, which the main agent approves; the Contractor's first delivery changes that acceptance
test and the hub refuses it; the second passes the tests but changes the client's `CONTRACT.md`
(flagged) and awaits the inspection of a constraint (CONCERNS); the Owner inspects it (PASS) and
accepts with reservations. A model call and a comment on the pull request fail on the way.

To write the file again after a change of the exporter or of the run below:

    uv run --locked python -m tests.unit.tools.replay_example
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.replay import export

from tests.unit.tools.replay_hub import HubFile, Message, write_transcript
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.contract import check_integrity, validate_object
from zephyros.core.deliveries import DeliveryRecord, DeliveryState
from zephyros.core.gate import Inspected, compute_gate
from zephyros.core.ports.verification import Outcome as TestOutcome
from zephyros.core.projects import Project

EXAMPLE = Path(export.__file__).with_name("example.replay.json")
PROJECT = "p_5e1f0c2a"
OWNER = f"owner:{PROJECT}"
CONTRACTOR = f"contractor:{PROJECT}"
OPERATOR = "operator"
MAIN_AGENT = "main-agent"
HUB = "hub"
START = datetime(2026, 9, 21, 14, 0, 0, tzinfo=UTC)
EXPORTED_AT = "2026-09-21T15:00:00.000000Z"
REPOSITORY = "https://example.invalid/status-svc"
MODEL = "served-model"
OPERATOR_SOURCE = "127.0.0.1"
OWNER_SOURCE = "203.0.113.10"
CONTRACTOR_SOURCE = "203.0.113.18"
"""The callers' addresses, from a documentation range: the exporter drops them all the same."""

LISTS_CHECKS = "test_status.py::test_lists_every_check"
ANSWERS_FAST = "test_status.py::test_answers_within_200_ms"

FIRST: dict[str, dict[str, object]] = {
    "EXG-01": {
        "type": "requirement",
        "id": "EXG-01",
        "text": "The service shall answer GET /status with the state of each check.",
        "priority": "P0",
        "methods": ["test"],
    },
    "EXG-02": {
        "type": "requirement",
        "id": "EXG-02",
        "text": "The service shall answer GET /status within 200 ms.",
        "priority": "P1",
        "methods": ["test"],
    },
    "CTR-01": {
        "type": "constraint",
        "id": "CTR-01",
        "text": "The service shall use the Python standard library only.",
        "priority": "P0",
        "methods": ["inspection"],
    },
    "US-E1-01": {
        "type": "story",
        "id": "US-E1-01",
        "epic": "E1",
        "title": "See the state of each check",
        "text": "As an operator, I want the state of each check, so that I know what is down.",
    },
    "US-E1-01.1": {
        "type": "criterion",
        "id": "US-E1-01.1",
        "story": "US-E1-01",
        "text": "GET /status lists every configured check with its state.",
        "test": LISTS_CHECKS,
    },
    "US-E1-02": {
        "type": "story",
        "id": "US-E1-02",
        "epic": "E1",
        "title": "A status page that answers fast",
        "text": "As an operator, I want the status at once, so that a dashboard can poll it.",
    },
    "LNK:US-E1-01>EXG-01": {
        "type": "link",
        "id": "LNK:US-E1-01>EXG-01",
        "kind": "covers",
        "source": "US-E1-01",
        "target": "EXG-01",
    },
    "LNK:US-E1-01>CTR-01": {
        "type": "link",
        "id": "LNK:US-E1-01>CTR-01",
        "kind": "covers",
        "source": "US-E1-01",
        "target": "CTR-01",
    },
    "LNK:US-E1-02>EXG-02": {
        "type": "link",
        "id": "LNK:US-E1-02>EXG-02",
        "kind": "covers",
        "source": "US-E1-02",
        "target": "EXG-02",
    },
}
CRITERION: dict[str, object] = {
    "type": "criterion",
    "id": "US-E1-02.1",
    "story": "US-E1-02",
    "text": "GET /status answers within 200 ms with ten checks configured.",
    "test": ANSWERS_FAST,
}
SECOND: dict[str, dict[str, object]] = {**FIRST, "US-E1-02.1": CRITERION}
OUTCOMES = {LISTS_CHECKS: "passed", ANSWERS_FAST: "passed"}

OBSERVATION = (
    "Read the delivered branch: status_svc/app.py imports http.server, json and time only; "
    "no third-party package anywhere."
)
REASONS = (
    "Both tests pass and CTR-01 conforms. Reservation: the delivery rewrites CONTRACT.md, the "
    "client's own file; that change is not accepted and must be reverted before the merge."
)


def _checked(objects: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    """A contract the hub would accept: every object valid, every reference resolved."""
    problems = [p for key, item in objects.items() for p in validate_object(item, key)]
    problems += check_integrity(objects)
    if problems:
        raise ValueError(f"the example contract is invalid: {problems}")
    return objects


def _decision(objects: dict[str, dict[str, object]], inspected: bool) -> str:
    """The decision of the gate itself, from the outcomes and the inspection of CTR-01."""
    outcomes = {test: TestOutcome(value) for test, value in OUTCOMES.items()}
    inspections = {"CTR-01": Inspected.CONFORMS} if inspected else None
    return compute_gate(objects, outcomes, inspections=inspections).decision.value


def write_hub(path: Path) -> None:
    """The run, record by record, as the hub would have written it."""
    hub = HubFile(path, Project(PROJECT, "status-svc", REPOSITORY, START, OPERATOR))
    operator = {"source": OPERATOR_SOURCE, "project_id": PROJECT}
    owner = {"source": OWNER_SOURCE, "project_id": PROJECT}
    contractor = {"source": CONTRACTOR_SOURCE, "project_id": PROJECT}

    def call(wait: float, principal: str, source: str, tokens: tuple[int, int, int]) -> None:
        prompt, completion, tool_calls = tokens
        hub.log(
            wait,
            principal,
            Action.GATEWAY_CALL,
            {
                "source": source,
                "model": MODEL,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "tool_calls": tool_calls,
            },
        )

    hub.log(
        0,
        OPERATOR,
        Action.PROJECT_CREATE,
        {**operator, "name": "status-svc", "has_repository_url": True},
    )
    # The operator imports the contract: the version is written, then the record.
    hub.clock.advance(4)
    first = hub.version(1, _checked(FIRST), OPERATOR)
    hub.log(
        0,
        OPERATOR,
        Action.CONTRACT_IMPORT,
        {**operator, "version_id": first, "objects": len(FIRST), "flagged": {}},
    )
    hub.log(
        6,
        OPERATOR,
        Action.PROJECT_PREPARE,
        {**operator, "repository": REPOSITORY, "branch": "main", "state": "preparing"},
    )
    hub.log(
        3,
        HUB,
        Action.STAGING_PREPARE,
        {"repository": REPOSITORY, "branch": "main", "base": "ba5eba5eba5e"},
    )

    # The Owner's first turn: it writes the acceptance test and proposes a criterion for EXG-02.
    hub.log(
        8,
        OPERATOR,
        Action.INSTANCE_START,
        {**operator, "side": "owner", "prompt_sha256": "aaaa1111bbbb", "prompt_chars": 1840},
    )
    hub.log(2, HUB, Action.TOKEN_ISSUE, {"token_id": "tok_example_owner", "side": "owner"})
    hub.log(
        1,
        HUB,
        Action.TURN_START,
        {"side": "owner", "prompt_chars": 1840, "prompt_sha256": "aaaa1111bbbb"},
    )
    hub.log(7, OWNER, Action.CONTRACT_READ, {**owner, "version_id": first})
    call(9, OWNER, OWNER_SOURCE, (9_840, 610, 2))
    hub.log(
        11,
        OWNER,
        Action.ACCEPTANCE_PUT,
        {**owner, "path": "test_status.py", "sha256": "5ca1ab1e5ca1ab1e5ca1"},
    )
    call(14, OWNER, OWNER_SOURCE, (12_310, 880, 1))
    hub.log(
        9,
        OWNER,
        Action.CONTRACT_PROPOSE,
        {
            **owner,
            "change_id": "c_7k2m9q",
            "base_version": 1,
            "operations": 1,
            "summary": "US-E1-02.1: EXG-02 gets an acceptance test of its own",
            "flagged": {},
        },
    )
    call(8, OWNER, OWNER_SOURCE, (13_020, 240, 0))
    hub.log(4, HUB, Action.TURN_END, {"side": "owner", "result": "completed"})

    # The main agent reads the pending change and approves it; the operator, a beat late, too.
    hub.log(17, MAIN_AGENT, Action.CONTRACT_PENDING, {**operator, "pending": 1})
    hub.clock.advance(6)
    second = hub.version(2, _checked(SECOND), MAIN_AGENT)
    hub.log(
        0,
        MAIN_AGENT,
        Action.CONTRACT_APPROVE,
        {**operator, "change_id": "c_7k2m9q", "approver": MAIN_AGENT, "version_id": second},
    )
    hub.log(
        9,
        OPERATOR,
        Action.CONTRACT_APPROVE,
        {**operator, "detail": "the change is already approved"},
        Outcome.REFUSED,
        Reason.NOT_PENDING,
    )

    # The Contractor's turn: two deliveries.
    hub.log(
        7,
        OPERATOR,
        Action.INSTANCE_START,
        {**operator, "side": "contractor", "prompt_sha256": "cccc2222dddd", "prompt_chars": 1520},
    )
    hub.log(
        2, HUB, Action.TOKEN_ISSUE, {"token_id": "tok_example_contractor", "side": "contractor"}
    )
    hub.log(
        1,
        HUB,
        Action.TURN_START,
        {"side": "contractor", "prompt_chars": 1520, "prompt_sha256": "cccc2222dddd"},
    )
    hub.log(6, CONTRACTOR, Action.CONTRACT_READ, {**contractor, "version_id": second})
    hub.log(5, CONTRACTOR, Action.STAGING_READ, {"source": CONTRACTOR_SOURCE, "bytes": 18_432})
    call(12, CONTRACTOR, CONTRACTOR_SOURCE, (8_120, 1_450, 3))
    hub.log(
        16,
        CONTRACTOR,
        Action.GATEWAY_CALL,
        {
            "source": CONTRACTOR_SOURCE,
            "model": MODEL,
            "detail": "the provider answered HTTP 503: the model is overloaded",
        },
        Outcome.ERROR,
    )
    call(21, CONTRACTOR, CONTRACTOR_SOURCE, (10_960, 2_380, 4))
    hub.log(
        9,
        CONTRACTOR,
        Action.ACCEPTANCE_PUT,
        {**contractor, "detail": "a contractor may not use acceptance.put here"},
        Outcome.REFUSED,
        Reason.FORBIDDEN,
    )
    first_push = hub.log(
        8,
        CONTRACTOR,
        Action.DELIVERY_PUSH,
        {
            "source": CONTRACTOR_SOURCE,
            "delivery_id": "d1",
            "branch": "status-endpoint",
            "bytes": 9_216,
            "changed": 2,
        },
    )
    hub.log(
        2,
        CONTRACTOR,
        Action.DELIVERY_DECLARE,
        {**contractor, "delivery_id": "d1", "state": "declared"},
    )
    hub.log(
        2,
        HUB,
        Action.DELIVERY_CHECK,
        {"delivery": "d1", "paths": 1},
        Outcome.REFUSED,
        Reason.PROTECTED_PATH,
    )
    hub.log(6, CONTRACTOR, Action.DELIVERY_STATUS, {**contractor, "delivery_id": "d1"})
    call(10, CONTRACTOR, CONTRACTOR_SOURCE, (12_740, 1_120, 2))
    second_push = hub.log(
        14,
        CONTRACTOR,
        Action.DELIVERY_PUSH,
        {
            "source": CONTRACTOR_SOURCE,
            "delivery_id": "d2",
            "branch": "status-endpoint-2",
            "bytes": 9_804,
            "changed": 3,
        },
    )
    hub.log(
        2,
        CONTRACTOR,
        Action.DELIVERY_DECLARE,
        {**contractor, "delivery_id": "d2", "state": "declared"},
    )
    hub.log(2, HUB, Action.DELIVERY_CHECK, {"delivery": "d2", "flagged": 1})
    hub.log(
        8,
        HUB,
        Action.VERIFICATION_RUN,
        {"delivery": "d2", "kit": "tests", "tests": len(OUTCOMES), "passed": len(OUTCOMES)},
    )
    concerns = _decision(SECOND, inspected=False)
    hub.log(
        1,
        HUB,
        Action.GATE_DECISION,
        {"delivery": "d2", "decision": concerns, "contract_version": 2},
    )
    hub.log(5, HUB, Action.PULL_REQUEST_OPEN, {"delivery": "d2", "number": 12})
    decided = {
        "event": "delivery.decided",
        "project_id": PROJECT,
        "delivery_id": "d2",
        "branch": "status-endpoint-2",
        "simulation": False,
    }
    hub.log(1, HUB, Action.NOTIFICATION, {**decided, "decision": concerns})
    hub.log(6, CONTRACTOR, Action.DELIVERY_STATUS, {**contractor, "delivery_id": "d2"})
    hub.log(5, HUB, Action.TURN_END, {"side": "contractor", "result": "completed"})

    # The Owner's second turn: it reads the delivered branch, inspects CTR-01 and reviews.
    hub.log(
        30,
        OPERATOR,
        Action.INSTANCE_START,
        {**operator, "side": "owner", "prompt_sha256": "eeee3333ffff", "prompt_chars": 960},
    )
    hub.log(
        1,
        HUB,
        Action.TURN_START,
        {"side": "owner", "prompt_chars": 960, "prompt_sha256": "eeee3333ffff"},
    )
    hub.log(9, OWNER, Action.DELIVERY_STATUS, {**owner, "delivery_id": "d2"})
    hub.log(
        8,
        OWNER,
        Action.STAGING_READ,
        {"source": OWNER_SOURCE, "delivery_id": "d2", "bytes": 9_804},
    )
    call(12, OWNER, OWNER_SOURCE, (15_480, 930, 2))
    # One request: the hub dates the inspection, the gate decides again (and the operator hears
    # of the new decision), then the call is logged.
    hub.clock.advance(15)
    inspected_at = hub.now()
    passed = _decision(SECOND, inspected=True)
    hub.log(
        0,
        HUB,
        Action.GATE_DECISION,
        {
            "delivery": "d2",
            "decision": passed,
            "previous": concerns,
            "contract_version": 2,
            "after": "inspection",
        },
    )
    hub.log(0, HUB, Action.NOTIFICATION, {**decided, "decision": passed})
    hub.log(
        0,
        OWNER,
        Action.INSPECTION_RECORD,
        {
            **owner,
            "delivery_id": "d2",
            "object_id": "CTR-01",
            "verdict": "conforms",
            "decision": passed,
        },
    )
    hub.log(4, HUB, Action.PULL_REQUEST_COMMENT, {"delivery": "d2", "number": 12})
    hub.log(1, HUB, Action.PULL_REQUEST_RETITLE, {"delivery": "d2", "number": 12})
    call(9, OWNER, OWNER_SOURCE, (16_200, 540, 1))
    hub.clock.advance(12)
    reviewed_at = hub.now()
    hub.log(
        0,
        OWNER,
        Action.DELIVERY_REVIEW,
        {
            **owner,
            "delivery_id": "d2",
            "verdict": "accept_with_reservations",
            "decision": passed,
        },
    )
    hub.log(
        8,
        HUB,
        Action.PULL_REQUEST_COMMENT,
        {"delivery": "d2", "error": "the forge answered HTTP 502"},
        Outcome.ERROR,
    )
    hub.log(5, HUB, Action.TURN_END, {"side": "owner", "result": "completed"})

    # The rows of the two deliveries, as they stand at the end of the run.
    hub.delivery(
        DeliveryRecord(
            project_id=PROJECT,
            delivery_id="d1",
            branch="status-endpoint",
            state=DeliveryState.REFUSED,
            received_at=first_push,
            changed_paths=("status_svc/app.py", "test_status.py"),
            detail="the delivery changes acceptance tests: test_status.py",
        )
    )
    hub.delivery(
        DeliveryRecord(
            project_id=PROJECT,
            delivery_id="d2",
            branch="status-endpoint-2",
            state=DeliveryState.PROPOSED,
            received_at=second_push,
            changed_paths=("CONTRACT.md", "status_svc/app.py", "tests/test_app.py"),
            decision=passed,
            outcomes=OUTCOMES,
            report="the evidence report",
            pull_request=f"{REPOSITORY}/pull/12",
            pull_number=12,
            contract_version=2,
            inspections={
                "CTR-01": {
                    "verdict": "conforms",
                    "observation": OBSERVATION,
                    "by": OWNER,
                    "at": inspected_at,
                }
            },
            review={
                "verdict": "accept_with_reservations",
                "reasons": REASONS,
                "by": OWNER,
                "at": reviewed_at,
            },
            flagged_paths=("CONTRACT.md",),
        )
    )
    hub.close()


def _at(seconds: float) -> float:
    """A time of the run, as Hermes stores it: seconds since the epoch."""
    return START.timestamp() + seconds


OWNER_MESSAGES: list[Message] = [
    ("s1", "user", "Write the acceptance tests of contract version 1.", _at(25)),
    (
        "s1",
        "assistant",
        "Contract version 1 read. EXG-02 has no acceptance test yet: I will write one and "
        "propose a criterion for it, so that the gate can verify it.",
        _at(42),
    ),
    ("s1", "tool", '{"path": "test_status.py", "sha256": "5ca1ab1e5ca1"}', _at(52)),
    (
        "s1",
        "assistant",
        "test_status.py is stored, and change c_7k2m9q proposes US-E1-02.1: GET /status answers "
        "within 200 ms. It awaits a decision.",
        _at(85),
    ),
    ("s2", "user", "Judge delivery d2: the gate awaits the inspection of CTR-01.", _at(301)),
    (
        "s2",
        "assistant",
        "Read the delivered branch. status_svc/app.py imports only the standard library: CTR-01 "
        "conforms. The delivery also rewrites CONTRACT.md.",
        _at(335),
    ),
    ("s2", "tool", '{"decision": "PASS"}', _at(345)),
    (
        "s2",
        "assistant",
        "Inspection recorded and the gate reads PASS. Verdict: accept with reservations, because "
        "CONTRACT.md is the client's own file.",
        _at(381),
    ),
]
CONTRACTOR_MESSAGES: list[Message] = [
    ("s1", "user", "Deliver contract version 2 on the staging area.", _at(129)),
    (
        "s1",
        "assistant",
        "The base has no status endpoint yet. Plan: GET /status in status_svc/app.py, with "
        "tests next to it.",
        _at(145),
    ),
    (
        "s1",
        "assistant",
        "Delivery d1 was refused: test_status.py is the path of an acceptance test. My tests "
        "move to tests/test_app.py and I deliver again.",
        _at(218),
    ),
    ("s1", "tool", '{"delivery_id": "d2", "state": "declared"}', _at(242)),
    (
        "s1",
        "assistant",
        "Delivered d2: both acceptance tests pass; the gate reads CONCERNS until CTR-01 is "
        "inspected.",
        _at(266),
    ),
]


def example_document(directory: Path) -> dict[str, Any]:
    """The example replay: the run above, exported as a run would be."""
    database = directory / "hub.db"
    write_hub(database)
    owner = write_transcript(directory / "owner-state.db", OWNER_MESSAGES)
    contractor = write_transcript(directory / "contractor-state.db", CONTRACTOR_MESSAGES)
    options = export.Options(
        db=database,
        project=PROJECT,
        kind="synthetic",
        title="A synthetic run: a status endpoint, two deliveries, one inspection",
        transcripts=(("owner", owner), ("contractor", contractor)),
    )
    return export.replay_document(options, exported_at=EXPORTED_AT)


def example_text(directory: Path) -> str:
    """The text of the example file."""
    return export.document_text(example_document(directory)) + "\n"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zephyros-example-") as directory:
        EXAMPLE.write_text(example_text(Path(directory)), encoding="utf-8")
    sys.stderr.write(f"wrote {EXAMPLE}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
