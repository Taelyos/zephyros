"""The replay exporter (docs/replay-format.md): what it keeps, drops, derives and totals.

The hub database is built with the project's own SQLite adapters and `AuditLog`, so the hashes
and the chain in the tests are the real ones.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tools.replay import export

from tests.unit.tools.replay_example import EXAMPLE, example_text
from tests.unit.tools.replay_hub import HubFile, write_transcript
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.contract import content_hash, version_id
from zephyros.core.deliveries import DeliveryRecord, DeliveryState
from zephyros.core.projects import Project

PROJECT = "p_abcdef12"
OWNER = f"owner:{PROJECT}"
CONTRACTOR = f"contractor:{PROJECT}"


def address(*octets: int) -> str:
    """A dotted IPv4 address, assembled here: this file holds no literal private address.

    The exporter's redaction is tested with private ranges (RFC 1918 and RFC 6598); building the
    strings from their octets keeps a private address out of the source of a public repository.
    """
    return ".".join(str(octet) for octet in octets)


OWNER_ADDRESS = address(10, 200, 0, 2)
CONTRACTOR_ADDRESS = address(10, 200, 0, 18)
START = datetime(2026, 3, 4, 9, 0, 0, tzinfo=UTC)
EXPORTED_AT = "2026-03-04T10:00:00.000000Z"

OBJECTS: dict[str, dict[str, object]] = {
    "EXG-01": {
        "type": "requirement",
        "id": "EXG-01",
        "text": "The service shall answer on GET /status.",
        "priority": "P0",
        "methods": ["test"],
    },
    "CTR-01": {
        "type": "constraint",
        "id": "CTR-01",
        "text": "The service shall use only the standard library.",
        "priority": "P0",
        "methods": ["inspection"],
    },
    "US-E1-01": {
        "type": "story",
        "id": "US-E1-01",
        "epic": "E1",
        "title": "to see the state",
        "text": "As a user I want the state.",
    },
    "US-E1-01.1": {
        "type": "criterion",
        "id": "US-E1-01.1",
        "story": "US-E1-01",
        "text": "The state shall be JSON.",
        "verification": "T",
    },
}

Audit = tuple[Action, Outcome, str, Reason, dict[str, object]]

# Twenty-one records, one per second from 09:00:00: a whole small run, from the contract import
# to the end of the Owner turn, with two model calls, one failed model call, a refusal, one
# delivery, two gate decisions (CONCERNS then PASS after an inspection) and a verdict.
RECORDS: list[Audit] = [
    (Action.PROJECT_CREATE, Outcome.OK, "operator", Reason.NONE, {"name": "alpha-svc"}),
    (
        Action.CONTRACT_IMPORT,
        Outcome.OK,
        "operator",
        Reason.NONE,
        {"objects": len(OBJECTS), "version_id": version_id(1, content_hash(OBJECTS))},
    ),
    (
        Action.INSTANCE_START,
        Outcome.OK,
        "operator",
        Reason.NONE,
        {"side": "owner", "prompt_chars": 1000, "prompt_sha256": "abc123", "source": "127.0.0.1"},
    ),
    (Action.TURN_START, Outcome.OK, "hub", Reason.NONE, {"side": "owner", "prompt_chars": 1000}),
    (
        Action.GATEWAY_CALL,
        Outcome.OK,
        OWNER,
        Reason.NONE,
        {
            "model": "served-model",
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "tool_calls": 2,
            "source": OWNER_ADDRESS,
        },
    ),
    (
        Action.GATEWAY_CALL,
        Outcome.OK,
        OWNER,
        Reason.NONE,
        {
            "model": "served-model",
            "prompt_tokens": 2000,
            "completion_tokens": 100,
            "tool_calls": 1,
            "source": OWNER_ADDRESS,
        },
    ),
    (
        Action.GATEWAY_CALL,
        Outcome.ERROR,
        OWNER,
        Reason.NONE,
        {
            "detail": "the provider answered HTTP 400",
            "model": "served-model",
            "source": OWNER_ADDRESS,
        },
    ),
    (
        Action.CONTRACT_PROPOSE,
        Outcome.OK,
        OWNER,
        Reason.NONE,
        {
            "base_version": 1,
            "change_id": "c_1a2b3c4d",
            "flagged": {},
            "operations": 1,
            "summary": "US-E1-01 gains a second criterion",
            "source": OWNER_ADDRESS,
        },
    ),
    (
        Action.UNKNOWN,
        Outcome.REFUSED,
        OWNER,
        Reason.NOT_FOUND,
        {"detail": f"no such path {address(192, 168, 1, 10)}", "source": OWNER_ADDRESS},
    ),
    (
        Action.ACCEPTANCE_PUT,
        Outcome.OK,
        OWNER,
        Reason.NONE,
        {"path": "test_status.py", "sha256": "deadbeefcafe", "source": OWNER_ADDRESS},
    ),
    (
        Action.GATEWAY_CALL,
        Outcome.OK,
        CONTRACTOR,
        Reason.NONE,
        {
            "model": "served-model",
            "prompt_tokens": 500,
            "completion_tokens": 250,
            "tool_calls": 0,
            "source": CONTRACTOR_ADDRESS,
        },
    ),
    (
        Action.DELIVERY_PUSH,
        Outcome.OK,
        CONTRACTOR,
        Reason.NONE,
        {
            "branch": "feature",
            "bytes": 100,
            "changed": 2,
            "delivery_id": "d1",
            "source": CONTRACTOR_ADDRESS,
        },
    ),
    (
        Action.DELIVERY_DECLARE,
        Outcome.OK,
        CONTRACTOR,
        Reason.NONE,
        {"delivery_id": "d1", "state": "declared", "source": CONTRACTOR_ADDRESS},
    ),
    (
        Action.VERIFICATION_RUN,
        Outcome.OK,
        "hub",
        Reason.NONE,
        {"delivery": "d1", "kit": "tests", "tests": 2, "passed": 2},
    ),
    (
        Action.GATE_DECISION,
        Outcome.OK,
        "hub",
        Reason.NONE,
        {"contract_version": 1, "decision": "CONCERNS", "delivery": "d1"},
    ),
    (Action.PULL_REQUEST_OPEN, Outcome.OK, "hub", Reason.NONE, {"delivery": "d1", "number": 7}),
    (
        Action.GATE_DECISION,
        Outcome.OK,
        "hub",
        Reason.NONE,
        {
            "after": "inspection",
            "contract_version": 1,
            "decision": "PASS",
            "delivery": "d1",
            "previous": "CONCERNS",
        },
    ),
    (
        Action.INSPECTION_RECORD,
        Outcome.OK,
        OWNER,
        Reason.NONE,
        {
            "decision": "PASS",
            "delivery_id": "d1",
            "object_id": "CTR-01",
            "verdict": "conforms",
            "source": OWNER_ADDRESS,
        },
    ),
    (
        Action.DELIVERY_REVIEW,
        Outcome.OK,
        OWNER,
        Reason.NONE,
        {
            "decision": "PASS",
            "delivery_id": "d1",
            "verdict": "accept_with_reservations",
            "source": OWNER_ADDRESS,
        },
    ),
    (Action.PULL_REQUEST_COMMENT, Outcome.OK, "hub", Reason.NONE, {"delivery": "d1", "number": 7}),
    (Action.TURN_END, Outcome.OK, "hub", Reason.NONE, {"result": "completed", "side": "owner"}),
]

# The transcript: the note quotes a closing script tag, so the player test can check that the
# exporter escapes it.
NOTE = (
    "Starting; note that </script> must not close the element, "
    f"and probing {address(10, 9, 8, 7)} now."
)
FINAL = "Done: the delivery is in and the gate reads PASS."
INSIDE_RUN = "a message from outside the exported range"
PUSH = 11  # the record that carried the delivery, one second per record from 09:00:00
INSPECTED = 16  # the gate decided again, then the inspection was logged: the row's time
REVIEWED = 18


def unix(second: int) -> float:
    return datetime(2026, 3, 4, 9, 0, second, tzinfo=UTC).timestamp()


def project(name: str = "alpha-svc") -> Project:
    return Project(PROJECT, name, "https://example.invalid/alpha-svc", START, "operator")


def build_hub(tmp_path: Path) -> Path:
    """A small hub database, where every record really was hashed by the hub's own log."""
    path = tmp_path / "hub.db"
    hub = HubFile(path, project())
    times = [
        hub.log(0 if index == 0 else 1, principal, action, parameters, outcome, reason)
        for index, (action, outcome, principal, reason, parameters) in enumerate(RECORDS)
    ]
    hub.clock.set(datetime.fromisoformat(times[1]))
    hub.version(1, OBJECTS, "operator")
    hub.delivery(
        DeliveryRecord(
            project_id=PROJECT,
            delivery_id="d1",
            branch="feature",
            state=DeliveryState.PROPOSED,
            received_at=times[PUSH],
            changed_paths=("alpha_svc/app.py", "CONTRACT.md"),
            decision="PASS",
            outcomes={"test_x.py::test_a": "passed", "test_x.py::test_b": "passed"},
            report="the evidence report",
            pull_request="https://example.invalid/alpha-svc/pull/7",
            pull_number=7,
            contract_version=1,
            inspections={
                "CTR-01": {
                    "verdict": "conforms",
                    "observation": f"stdlib only, checked on {address(10, 0, 0, 5)}",
                    "by": OWNER,
                    "at": times[INSPECTED],
                }
            },
            review={
                "verdict": "accept_with_reservations",
                "reasons": f"all accepted; the probe came from {address(100, 64, 12, 7)}",
                "by": OWNER,
                "at": times[REVIEWED],
            },
        )
    )
    hub.close()
    return path


def build_transcript(path: Path) -> Path:
    """A tiny Hermes state database: the Owner's conversation, with two answers in the run."""
    return write_transcript(
        path,
        [
            ("s1", "user", "make the delivery", unix(0)),
            ("s1", "assistant", NOTE, unix(2)),
            ("s1", "tool", "ok", unix(3)),
            ("s1", "assistant", "", unix(4)),
            ("s1", "assistant", FINAL, unix(5)),
            ("s1", "user", "thank you", unix(6)),
            ("s1", "assistant", INSIDE_RUN, unix(0) - 3600),
            ("s1", "assistant", INSIDE_RUN, unix(0) + 3600),
        ],
    )


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    return build_hub(tmp_path)


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return build_transcript(tmp_path / "state.db")


def replay(hub: Path, **overrides: Any) -> dict[str, Any]:
    options = export.Options(db=hub, project=PROJECT, title="a synthetic run", **overrides)
    return export.replay_document(options, exported_at=EXPORTED_AT)


def of_type(document: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [event for event in document["events"] if event["type"] == action]


def only(document: dict[str, Any], action: str) -> dict[str, Any]:
    found = of_type(document, action)
    assert len(found) == 1, (action, len(found))
    return found[0]


def tamper(path: Path, sql: str) -> None:
    """An attacker who owns the file drops a trigger and edits or removes a record."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TRIGGER audit_records_no_update")
        db.execute("DROP TRIGGER audit_records_no_delete")
        db.execute(sql)


# ----------------------------------------------------------------------------- the chain
def test_every_exported_record_is_verified_against_the_chain(hub: Path) -> None:
    document = replay(hub)
    assert document["integrity"] == {
        "chain": "verified",
        "records": len(RECORDS),
        "first_previous": "0" * 64,
        "last_hash": document["integrity"]["last_hash"],
        "detail": None,
    }
    assert len(document["integrity"]["last_hash"]) == 64
    assert document["run"]["from_seq"] == 1
    assert document["run"]["to_seq"] == len(RECORDS)
    assert document["totals"]["duration_ms"] == (len(RECORDS) - 1) * 1000


def test_an_edited_record_breaks_the_chain(hub: Path) -> None:
    tamper(hub, "UPDATE audit_records SET principal = 'operator' WHERE seq = 5")
    integrity = replay(hub)["integrity"]
    assert integrity["chain"] == "broken"
    assert integrity["detail"] == "record 5 does not match its hash"


def test_a_missing_record_breaks_the_link_and_the_first_previous_is_reported(hub: Path) -> None:
    tamper(hub, "DELETE FROM audit_records WHERE seq = 3")
    integrity = replay(hub)["integrity"]
    assert integrity["chain"] == "broken"
    assert integrity["detail"] == "record 4 does not link to record 2"
    assert integrity["first_previous"] == "0" * 64


def test_only_hub_log_events_are_numbered_by_the_log(hub: Path) -> None:
    document = replay(hub, transcripts=(("owner", build_transcript(hub.parent / "state.db")),))
    for event in document["events"]:
        assert (event["seq"] is not None) == (event["source"] == "hub-log")


# ----------------------------------------------------------------------------- data and hub_state
DERIVED = {"reason", "action", "detail", "side", "cost_usd", "version"}
"""What the exporter adds to a record's parameters, from the record alone."""


def test_the_data_of_a_log_event_is_what_its_record_holds(hub: Path) -> None:
    """Nothing the tables hold reaches `data`: a reader can check it against the chain."""
    document = replay(hub)
    with export.Hub(hub) as view:
        records = {record.seq: record for record in view.records(PROJECT, None, None)}
    clean = export.Redactor.of(())
    for event in document["events"]:
        if event["source"] != "hub-log":
            continue
        parameters = clean.value(export.kept(records[event["seq"]].parameters))
        assert set(event["data"]) - set(parameters) <= DERIVED, event["type"]
        for key, value in parameters.items():
            assert event["data"][key] == value, (event["type"], key)


def test_what_the_tables_add_goes_to_hub_state(hub: Path) -> None:
    document = replay(hub)
    assert only(document, "delivery.push")["hub_state"] == {
        "changed_paths": ["alpha_svc/app.py", "CONTRACT.md"]
    }
    outcomes = {"test_x.py::test_a": "passed", "test_x.py::test_b": "passed"}
    assert [event["hub_state"] for event in of_type(document, "gate.decision")] == [
        {"outcomes": outcomes},
        {"outcomes": outcomes},
    ]
    assert only(document, "inspection.record")["hub_state"] == {
        "observation": "stdlib only, checked on [redacted]"
    }
    assert only(document, "delivery.review")["hub_state"] == {
        "reasons": "all accepted; the probe came from [redacted]"
    }
    url = {"url": "https://example.invalid/alpha-svc/pull/7"}
    assert only(document, "pull_request.open")["hub_state"] == url
    assert only(document, "pull_request.comment")["hub_state"] == url


def test_an_event_before_a_state_was_written_gets_none_of_it(hub: Path) -> None:
    """The row keeps the last state of a delivery: an earlier event must not show it."""
    document = replay(hub)
    for action in ("delivery.declare", "verification.run", "turn.end"):
        assert "hub_state" not in only(document, action), action
    push = only(document, "delivery.push")
    assert "url" not in push["hub_state"]


def test_a_state_read_does_not_show_the_final_state(tmp_path: Path) -> None:
    hub = HubFile(tmp_path / "hub.db", project())
    hub.log(0, CONTRACTOR, Action.DELIVERY_STATUS, {"project_id": PROJECT, "delivery_id": "d1"})
    hub.delivery(
        DeliveryRecord(PROJECT, "d1", "feature", DeliveryState.PROPOSED, hub.now(), decision="PASS")
    )
    hub.close()
    event = only(replay(tmp_path / "hub.db"), "delivery.status")
    assert event["data"] == {"project_id": PROJECT, "delivery_id": "d1"}
    assert "hub_state" not in event


def inspected_twice(tmp_path: Path, last_at: int) -> Path:
    """Two inspections of CTR-01 on d1; the row keeps the one dated `last_at` seconds in."""
    path = tmp_path / "hub.db"
    hub = HubFile(path, project())
    parameters = {"delivery_id": "d1", "object_id": "CTR-01", "verdict": "does_not_conform"}
    hub.log(0, OWNER, Action.INSPECTION_RECORD, parameters)
    hub.log(10, OWNER, Action.INSPECTION_RECORD, {**parameters, "verdict": "conforms"})
    hub.log(10, "hub", Action.TURN_END, {"side": "owner", "result": "completed"})
    stored = {"verdict": "conforms", "observation": "the second look", "by": OWNER}
    hub.delivery(
        DeliveryRecord(
            PROJECT,
            "d1",
            "feature",
            DeliveryState.PROPOSED,
            hub.now(),
            decision="PASS",
            contract_version=1,
            inspections={"CTR-01": {**stored, "at": f"2026-03-04T09:00:{last_at:02d}.000000Z"}},
        )
    )
    hub.close()
    return path


def test_an_inspection_recorded_again_lends_its_observation_to_its_own_record(
    tmp_path: Path,
) -> None:
    first, second = of_type(replay(inspected_twice(tmp_path, last_at=10)), "inspection.record")
    assert "hub_state" not in first
    assert second["hub_state"] == {"observation": "the second look"}


def test_an_inspection_recorded_after_the_range_lends_nothing(tmp_path: Path) -> None:
    for event in of_type(replay(inspected_twice(tmp_path, last_at=25)), "inspection.record"):
        assert "hub_state" not in event


# ----------------------------------------------------------------------------- actors and refusals
def test_each_principal_becomes_its_actor(hub: Path) -> None:
    document = replay(hub)
    assert document["events"][0]["actor"] == "operator"
    assert {event["actor"] for event in of_type(document, "gateway.call")} == {
        "owner",
        "contractor",
    }
    assert only(document, "delivery.push")["actor"] == "contractor"
    assert only(document, "verification.run")["actor"] == "hub"


def test_a_refusal_keeps_its_reason_its_action_and_its_category(hub: Path) -> None:
    event = only(replay(hub), "unknown")
    assert event["category"] == "refusal"
    assert event["outcome"] == "refused"
    assert event["data"]["reason"] == "not_found"
    assert event["data"]["action"] == "unknown"
    assert event["summary"] == "Refused (not_found): no such path [redacted]"


def test_the_caller_address_and_the_token_identifiers_are_dropped(hub: Path) -> None:
    document = replay(hub)
    text = export.document_text(document)
    assert OWNER_ADDRESS not in text
    assert CONTRACTOR_ADDRESS not in text
    assert '"token_id"' not in text
    for event in document["events"]:
        assert "source" not in event["data"]


# ----------------------------------------------------------------------------- summaries
PRICES = export.Prices(1.0, 2.0, "test rates")


def record(
    action: str,
    parameters: dict[str, object],
    outcome: str = "ok",
    principal: str = "hub",
    reason: str = "none",
) -> export.LogRecord:
    return export.LogRecord(
        seq=1,
        time="2026-03-04T09:00:00.000000Z",
        principal=principal,
        action=action,
        outcome=outcome,
        reason=reason,
        parameters=parameters,
        parameters_text=json.dumps(parameters),
        previous="0" * 64,
        hash="0" * 64,
    )


SUMMARIES: list[tuple[export.LogRecord, str, str]] = [
    (
        record("verification.run", {"delivery": "d2", "error": "the kit timed out"}, "error"),
        "delivery",
        "Verification of d2 failed: the kit timed out",
    ),
    (
        record("pull_request.open", {"delivery": "d2", "error": "HTTP 502"}, "error"),
        "delivery",
        "Hub cannot open a pull request for d2: HTTP 502",
    ),
    (
        record("pull_request.comment", {"delivery": "d2", "error": "HTTP 502"}, "error"),
        "delivery",
        "Hub cannot comment on the pull request for d2: HTTP 502",
    ),
    (
        record("pull_request.retitle", {"delivery": "d2", "number": 12}),
        "delivery",
        "Hub retitles pull request #12 for d2",
    ),
    (
        record("pull_request.retitle", {"delivery": "d2", "error": "HTTP 404"}, "error"),
        "delivery",
        "Hub cannot retitle the pull request for d2: HTTP 404",
    ),
    (
        record("turn.start", {"side": "owner", "error": "image_missing"}, "error"),
        "turn",
        "Owner turn cannot start: image_missing",
    ),
    (
        record("turn.start", {"side": "contractor", "prompt_chars": 12_345}),
        "turn",
        "Contractor turn begins (12,345 prompt chars)",
    ),
    (
        record("turn.end", {"side": "owner", "result": "guard_lost"}, "error"),
        "turn",
        "Owner turn ends in error: guard_lost",
    ),
    (
        record("instance.stop", {"side": "contractor"}, principal="operator"),
        "other",
        "Operator stops the Contractor instance",
    ),
    (
        record(
            "notification.sent",
            {"event": "delivery.decided", "delivery_id": "d2", "decision": "PASS"},
        ),
        "other",
        "Hub notifies the operator: delivery.decided for d2 (PASS)",
    ),
    (
        record(
            "notification.sent",
            {"event": "delivery.decided", "delivery_id": "d2", "error": "status 500"},
            "error",
        ),
        "other",
        "Hub cannot notify the operator of delivery.decided for d2: status 500",
    ),
    (
        record(
            "notification.sent",
            {"event": "delivery.loop", "branch": "feature", "rejections": 3},
        ),
        "other",
        "Hub notifies the operator: delivery.loop on feature",
    ),
    (
        record(
            "notification.sent",
            {"side": "owner", "step": "alert_human", "task": "decision", "error": "egress"},
            "error",
        ),
        "other",
        "Hub cannot notify the operator of alert human, Owner side, decision awaited: egress",
    ),
    (
        record("continuity.step", {"side": "contractor", "step": "wake", "task": "delivery"}),
        "other",
        "Continuity: wake, Contractor side, delivery awaited",
    ),
    (
        record("token.issue", {"token_id": "t_1", "side": "owner"}),
        "other",
        "Hub issues a token to the Owner side",
    ),
    (
        record("token.revoke", {"side": "owner", "count": 2, "why": "stop"}),
        "other",
        "Hub revokes 2 tokens of the Owner side (stop)",
    ),
    (
        record("staging.prepare", {"error": "the forge answered HTTP 404"}, "error"),
        "delivery",
        "Hub cannot prepare the staging area: the forge answered HTTP 404",
    ),
    (
        record("staging.prepare", {"repository": "r", "branch": "main", "base": "0a0a0a0a0a0a"}),
        "delivery",
        "Hub prepares the staging area from r main at 0a0a0a0a0a0a",
    ),
    (
        record("staging.read", {"bytes": 18_432}, principal=CONTRACTOR),
        "delivery",
        "Contractor reads the base from the staging area (18,432 bytes)",
    ),
    (
        record("staging.read", {"delivery_id": "d2", "bytes": 1}, principal=OWNER),
        "delivery",
        "Owner reads delivery d2 from the staging area (1 byte)",
    ),
    (
        record("delivery.status", {"delivery_id": "d2"}, principal=OWNER),
        "delivery",
        "Owner reads the state of d2",
    ),
    (
        record("delivery.check", {"delivery": "d2", "flagged": 1}),
        "flag",
        "Hub flags delivery d2: it changes 1 protected path",
    ),
    (
        record(
            "delivery.check",
            {"delivery": "d1", "paths": 2},
            "refused",
            reason="protected_path",
        ),
        "refusal",
        "Refused (protected_path): delivery d1 changes 2 acceptance test files",
    ),
    (
        record(
            "delivery.check",
            {"delivery": "d1", "paths": ["checks/add.txt", "checks/sub.txt"], "count": 2},
            "refused",
            reason="protected_path",
        ),
        "refusal",
        "Refused (protected_path): delivery d1 changes 2 acceptance test files: "
        "checks/add.txt, checks/sub.txt",
    ),
    (
        record(
            "delivery.check",
            {"delivery": "d1", "paths": ["checks/add.txt"], "count": 21},
            "refused",
            reason="protected_path",
        ),
        "refusal",
        "Refused (protected_path): delivery d1 changes 21 acceptance test files: "
        "checks/add.txt, ...",
    ),
    (
        record(
            "delivery.check",
            {
                "delivery": "d2",
                "flagged": 0,
                "missing": ["checks/add.txt", "checks/sub.txt"],
                "set_aside": ["checks/add.txt"],
            },
        ),
        "delivery",
        "Hub checks delivery d2: 2 acceptance test files the contract names are missing; "
        "1 delivered file in their place is set aside",
    ),
    (
        record(
            "delivery.check",
            {"delivery": "d2", "flagged": 1, "missing": ["checks/add.txt"], "set_aside": []},
        ),
        "flag",
        "Hub flags delivery d2: it changes 1 protected path; "
        "1 acceptance test file the contract names is missing",
    ),
    (
        record(
            "egress.refused",
            {"host": "hooks.example.invalid", "purpose": "notification"},
            "refused",
            reason="egress",
        ),
        "refusal",
        "Refused (egress): notification to hooks.example.invalid",
    ),
    (
        record("contract.read", {"version_id": "v3-0a0a0a0a0a0a"}, principal=CONTRACTOR),
        "contract",
        "Contractor reads contract version 3",
    ),
    (
        record(
            "contract.approve",
            {"change_id": "c_1", "version_id": "v2-0a0a0a0a0a0a"},
            principal="main-agent",
        ),
        "contract",
        "Main agent approves change c_1: contract version 2",
    ),
    (
        record("contract.reject", {"change_id": "c_1"}, principal="operator"),
        "contract",
        "Operator rejects change c_1",
    ),
    (
        record("contract.pending", {"pending": 2}, principal=OWNER),
        "contract",
        "Owner reads the pending changes (2)",
    ),
    (
        record(
            "contract.apply_reserved",
            {"version_id": "v4-0a0a0a0a0a0a", "summary": "P0 floor"},
            principal="operator",
        ),
        "contract",
        "Operator applies a reserved change as version 4: P0 floor",
    ),
    (
        record(
            "contract.import", {"version_id": "v1-0a0a0a0a0a0a", "objects": 1}, principal="operator"
        ),
        "contract",
        "Operator imports the contract as version 1 (1 object)",
    ),
    (
        record("project.create", {"name": "alpha-svc"}, principal="operator"),
        "other",
        "Operator creates project alpha-svc",
    ),
    (
        record("project.prepare", {"repository": "r", "branch": "main"}, principal="operator"),
        "other",
        "Operator asks for a staging area from r main",
    ),
    (
        record("gate.decision", {"delivery": "d2", "decision": "FAIL"}),
        "gate",
        "Gate: FAIL for d2",
    ),
    (
        record("log.read", {"stream": "hub"}, principal="operator"),
        "other",
        "Operator log.read",
    ),
    (
        record("log.verify", {"error": "the store is locked"}, "error", principal="operator"),
        "other",
        "Operator log.verify failed: the store is locked",
    ),
    (
        record("pull_request.open", {"delivery": "d2"}),
        "delivery",
        "Hub opens pull request #? for d2",
    ),
]


@pytest.mark.parametrize(
    ("logged", "category", "summary"),
    SUMMARIES,
    ids=[f"{item[0].action}-{item[0].outcome}-{index}" for index, item in enumerate(SUMMARIES)],
)
def test_each_record_says_what_happened(
    logged: export.LogRecord, category: str, summary: str
) -> None:
    event = export.hub_log_event(logged, PRICES)
    assert (event.category, event.summary) == (category, summary)
    assert event.outcome == logged.outcome


def test_a_failed_model_call_has_no_tokens_and_no_cost() -> None:
    logged = record("gateway.call", {"model": "m", "detail": "HTTP 503"}, "error", OWNER)
    event = export.hub_log_event(logged, PRICES)
    assert event.summary == "Model call failed: HTTP 503"
    assert event.data == {"model": "m", "detail": "HTTP 503", "side": "owner"}


def test_a_decision_after_an_inspection_says_what_it_was(hub: Path) -> None:
    second = of_type(replay(hub), "gate.decision")[1]
    assert second["summary"] == (
        "Gate: PASS for d1 (contract v1), after an inspection; it was CONCERNS"
    )


# ----------------------------------------------------------------------------- pricing and totals
def test_the_cost_of_every_model_call_and_the_totals(hub: Path) -> None:
    document = replay(hub)
    calls = of_type(document, "gateway.call")
    assert [event["data"].get("cost_usd") for event in calls] == [
        0.00045,
        0.00036,
        None,
        0.000225,
    ]
    assert [event["summary"] for event in calls] == [
        "Model call: 1,000 in / 500 out, 2 tool calls",
        "Model call: 2,000 in / 100 out, 1 tool call",
        "Model call failed: the provider answered HTTP 400",
        "Model call: 500 in / 250 out, 0 tool calls",
    ]
    assert document["totals"]["model_calls"] == {"contractor": 1, "owner": 2}
    assert document["totals"]["tokens"] == {
        "contractor": {"input": 500, "output": 250},
        "owner": {"input": 3000, "output": 600},
    }
    assert document["totals"]["cost_usd"] == {
        "contractor": 0.000225,
        "owner": 0.00081,
        "total": 0.001035,
    }
    assert document["totals"]["refusals"] == {"owner": 1}


def test_the_totals_name_the_sides_that_called_and_every_actor_refused(tmp_path: Path) -> None:
    """A refusal of the hub or of the operator adds no empty side to the costs."""
    hub = HubFile(tmp_path / "hub.db", project())
    hub.log(0, "hub", Action.EGRESS, {"host": "h"}, Outcome.REFUSED, Reason.EGRESS)
    hub.log(1, "operator", Action.CONTRACT_APPROVE, {}, Outcome.REFUSED, Reason.NOT_PENDING)
    hub.log(1, CONTRACTOR, Action.GATEWAY_CALL, {"prompt_tokens": 10, "completion_tokens": 1})
    hub.close()
    totals = replay(tmp_path / "hub.db")["totals"]
    assert totals["model_calls"] == {"contractor": 1}
    assert set(totals["tokens"]) == {"contractor"}
    assert set(totals["cost_usd"]) == {"contractor", "total"}
    assert totals["refusals"] == {"hub": 1, "operator": 1}


def test_the_pricing_comes_from_the_command_line(hub: Path) -> None:
    document = replay(hub, input_price=3.0, output_price=15.0, price_note="a price list")
    assert document["pricing"] == {
        "input_per_million": 3.0,
        "output_per_million": 15.0,
        "currency": "USD",
        "note": "a price list",
    }
    assert document["totals"]["cost_usd"]["owner"] == 0.018


def test_the_price_note_says_where_the_rates_come_from(hub: Path) -> None:
    assert replay(hub)["pricing"]["note"] == export.DEFAULT_PRICE_NOTE
    assert replay(hub, input_price=1.0)["pricing"]["note"] == export.GIVEN_PRICE_NOTE


def test_the_decisions_and_the_verdicts_are_totalled_in_order(hub: Path) -> None:
    totals = replay(hub)["totals"]
    assert [item["decision"] for item in totals["decisions"]] == ["CONCERNS", "PASS"]
    assert {item["delivery_id"] for item in totals["decisions"]} == {"d1"}
    assert totals["decisions"][0]["t"] < totals["decisions"][1]["t"]
    assert totals["verdicts"] == [
        {
            "delivery_id": "d1",
            "verdict": "accept_with_reservations",
            "t": only(replay(hub), "delivery.review")["t"],
        }
    ]


# ----------------------------------------------------------------------------- the contract
def test_the_contract_versions_become_events(hub: Path) -> None:
    document = replay(hub)
    event = only(document, "contract.version")
    assert event["source"] == "hub-state"
    assert event["seq"] is None
    assert event["actor"] == "operator"
    assert event["data"]["number"] == 1
    assert event["data"]["author"] == "operator"
    objects = {item["id"]: item for item in event["data"]["objects"]}
    assert objects["EXG-01"] == {
        "id": "EXG-01",
        "type": "requirement",
        "title": None,
        "text": "The service shall answer on GET /status.",
        "priority": "P0",
        "verification": "test",
        "test": None,
    }
    assert objects["US-E1-01.1"]["verification"] == "T"
    assert objects["US-E1-01.1"]["priority"] is None


def test_a_criterion_keeps_the_test_that_verifies_it() -> None:
    criterion = {
        "type": "criterion",
        "id": "US-E1-01.2",
        "story": "US-E1-01",
        "text": "The state shall list every check.",
        "test": "test_status.py::test_lists_every_check",
    }
    shaped = export.contract_object("US-E1-01.2", criterion)
    assert (shaped["verification"], shaped["test"]) == (
        None,
        "test_status.py::test_lists_every_check",
    )


def test_the_contract_is_the_one_in_force_at_the_end(hub: Path) -> None:
    contract = replay(hub)["contract"]
    assert contract["version"] == 1
    assert contract["version_id"] == version_id(1, content_hash(OBJECTS))
    assert len(contract["objects"]) == len(OBJECTS)


def test_the_contract_at_the_start_is_the_one_before_the_first_event(hub: Path) -> None:
    """The run imports its contract after its first record: it starts with none."""
    assert replay(hub)["contract_at_start"] == {"version": None, "version_id": None, "objects": []}
    later = replay(hub, from_seq=5)
    assert later["contract_at_start"]["version"] == 1
    assert later["contract_at_start"] == later["contract"]


def test_an_empty_range_has_no_event_and_the_last_contract(hub: Path) -> None:
    document = replay(hub, from_seq=1000)
    assert document["events"] == []
    assert document["totals"] == export.empty_totals()
    assert document["contract_at_start"]["version"] is None
    assert document["contract"]["version"] == 1
    assert document["integrity"]["records"] == 0


# ----------------------------------------------------------------------------- flags
def test_a_delivery_of_a_run_older_than_the_rule_is_flagged_by_the_exporter(hub: Path) -> None:
    event = only(replay(hub), "delivery.flag")
    assert event["category"] == "flag"
    assert event["source"] == "hub-state"
    assert event["data"] == {
        "delivery_id": "d1",
        "paths": ["CONTRACT.md"],
        "rule": "protected-path",
        "by": "exporter",
    }


def test_a_flag_of_the_hub_is_its_check_record_and_nothing_more(tmp_path: Path) -> None:
    """The hub records the flag when it verifies: the exporter adds no second one at the push."""
    hub = HubFile(tmp_path / "hub.db", project())
    pushed = hub.log(0, CONTRACTOR, Action.DELIVERY_PUSH, {"delivery_id": "d1", "changed": 1})
    hub.log(5, "hub", Action.DELIVERY_CHECK, {"delivery": "d1", "flagged": 1})
    hub.delivery(
        DeliveryRecord(
            PROJECT,
            "d1",
            "feature",
            DeliveryState.DECIDED,
            pushed,
            changed_paths=("CONTRACT.md",),
            flagged_paths=("CONTRACT.md",),
        )
    )
    hub.close()
    document = replay(tmp_path / "hub.db")
    assert of_type(document, "delivery.flag") == []
    check = only(document, "delivery.check")
    assert check["category"] == "flag"
    assert check["data"] == {"delivery_id": "d1", "flagged": 1}
    assert check["hub_state"] == {"paths": ["CONTRACT.md"]}


def test_a_delivery_that_touches_nothing_protected_is_not_flagged() -> None:
    body: dict[str, Any] = {
        "received_at": "2026-03-04T09:00:11.000000Z",
        "changed_paths": ["src/app.py"],
    }
    assert export.flagged_event("d1", body, ("CONTRACT.md",)) is None
    assert export.flagged_event("d1", {}, ("CONTRACT.md",)) is None


def test_a_protected_directory_covers_what_is_under_it() -> None:
    body: dict[str, Any] = {
        "received_at": "2026-03-04T09:00:11.000000Z",
        "changed_paths": ["acceptance/test_a.py", "src/app.py"],
    }
    event = export.flagged_event("d1", body, ("acceptance/",))
    assert event is not None
    assert event.data["paths"] == ["acceptance/test_a.py"]


# ----------------------------------------------------------------------------- redaction
def test_private_addresses_are_replaced_and_listed(hub: Path) -> None:
    document = replay(hub)
    text = export.document_text(document)
    assert address(192, 168, 1, 10) not in text
    assert address(10, 0, 0, 5) not in text
    assert export.REDACTED in text
    assert document["run"]["redactions"] == ["private IPv4 addresses"]


def test_a_pattern_of_the_command_line_is_replaced_and_never_written(hub: Path) -> None:
    """`run.redactions` names the kind; the pattern itself may be the very name to hide."""
    pattern = r"no\s+such\s+path"
    document = replay(hub, redact=(pattern,))
    assert document["run"]["redactions"] == ["--redact patterns", "private IPv4 addresses"]
    assert only(document, "unknown")["summary"] == "Refused (not_found): [redacted] [redacted]"
    assert pattern not in export.document_text(document)


def test_the_carrier_grade_range_is_private_too() -> None:
    redactor = export.Redactor.of(())
    below, above = address(100, 63, 0, 1), address(100, 128, 0, 1)
    inside = " ".join([below, address(100, 64, 0, 1), address(100, 127, 255, 254), above])
    assert redactor.text(inside) == " ".join([below, export.REDACTED, export.REDACTED, above])
    assert redactor.replaced == {"private IPv4 addresses"}


def token(prefix: str, length: int = 24) -> str:
    """A string shaped like a token, built here so that no literal one is in the source."""
    return prefix + "a" * length


def test_a_token_shaped_secret_is_masked_in_every_text(tmp_path: Path) -> None:
    """The hub masks its log (EXG-49); a transcript, an observation or a contract it never
    logged goes through the same mask."""
    path = tmp_path / "hub.db"
    hub = HubFile(path, project())
    hub.log(0, OWNER, Action.INSPECTION_RECORD, {"delivery_id": "d1", "object_id": "CTR-01"})
    hub.version(1, {"EXG-01": {**OBJECTS["EXG-01"], "text": f"key {token('sk-')}"}}, "operator")
    hub.delivery(
        DeliveryRecord(
            PROJECT,
            "d1",
            "feature",
            DeliveryState.PROPOSED,
            hub.now(),
            inspections={
                "CTR-01": {"observation": f"saw {token('ghp_')}", "at": hub.now(), "by": OWNER}
            },
        )
    )
    hub.close()
    transcript = write_transcript(
        tmp_path / "state.db",
        [("s1", "assistant", f"the key is {token('AKIA', 16).upper()}", unix(0))],
    )
    document = replay(path, transcripts=(("owner", transcript),))
    text = export.document_text(document)
    for prefix in ("sk-", "ghp_", "AKIA"):
        assert prefix not in text, prefix
    assert document["run"]["redactions"] == ["token-shaped secrets"]
    assert only(document, "inspection.record")["hub_state"] == {"observation": "saw [masked]"}


def test_the_keys_of_a_dictionary_are_redacted_and_renamed() -> None:
    redactor = export.Redactor.of((), (("alpha-svc", "service-1"),))
    outcomes = {
        f"test_reach[{address(10, 0, 0, 1)}]": "passed",
        f"test_reach[{address(10, 0, 0, 2)}]": "failed",
        "test_alpha-svc_up": "passed",
    }
    assert redactor.value({"outcomes": outcomes}) == {
        "outcomes": {
            "test_reach[[redacted]]": "passed",
            "test_reach[[redacted]] (2)": "failed",
            "test_service-1_up": "passed",
        }
    }
    assert redactor.replaced == {"private IPv4 addresses", "renamed names"}


def test_a_rename_never_changes_the_name_of_a_field(hub: Path) -> None:
    """`delivery` is in `delivery_id`, and `_` ends a name: the field must stay a field."""
    document = replay(hub, renames=(("delivery", "item"),))
    assert only(document, "delivery.declare")["data"]["delivery_id"] == "d1"
    assert {item["delivery_id"] for item in document["totals"]["decisions"]} == {"d1"}
    assert only(document, "delivery.declare")["summary"] == "Contractor declares item d1"
    redactor = export.Redactor.of((), (("alpha-svc", "service-1"),))
    assert redactor.value({"alpha-svc_id": "alpha-svc"}) == {"alpha-svc_id": "service-1"}


# ----------------------------------------------------------------------------- renaming
def test_a_name_is_replaced_by_a_neutral_one(hub: Path) -> None:
    document = replay(hub, renames=(("alpha-svc", "service-1"), ("feature", "branch-1")))
    text = export.document_text(document)
    assert "alpha-svc" not in text
    assert "feature" not in text
    assert document["run"]["project"]["name"] == "service-1"
    assert (
        only(document, "delivery.push")["summary"]
        == "Contractor pushes d1 on branch-1 (2 changed paths)"
    )
    assert only(document, "delivery.push")["data"]["branch"] == "branch-1"
    assert "renamed names" in document["run"]["redactions"]
    assert all("alpha-svc" not in entry for entry in document["run"]["redactions"])


def test_a_rename_is_case_insensitive_and_matches_whole_words() -> None:
    redactor = export.Redactor.of((), (("alpha-svc", "service-1"),))
    assert redactor.text("Alpha-Svc, ALPHA-SVC and alpha-svc") == (
        "service-1, service-1 and service-1"
    )
    assert redactor.text("myalpha-svc alpha-svc2") == "myalpha-svc alpha-svc2"
    assert redactor.replaced == {"renamed names"}


def test_an_underscore_ends_a_name_for_a_rename() -> None:
    """A test identifier or a variable carries a name between underscores: it is replaced."""
    redactor = export.Redactor.of((), (("prometheus", "service-1"),))
    assert redactor.text("test_prometheus_status PROMETHEUS_URL _prometheus") == (
        "test_service-1_status service-1_URL _service-1"
    )


def test_a_rename_reaches_the_transcript(hub: Path, transcript: Path) -> None:
    document = replay(
        hub, transcripts=(("owner", transcript),), renames=(("Starting", "service-9"),)
    )
    assert of_type(document, "agent.message")[0]["data"]["text"].startswith("service-9; note that")


def test_a_rename_file_is_read_with_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "names.txt"
    path.write_text(
        "# the services of the run\n\nalpha-svc=service-1\nbeta-svc = service-2\n",
        encoding="utf-8",
    )
    assert export.read_renames(path) == [("alpha-svc", "service-1"), ("beta-svc", "service-2")]


def test_a_rename_file_replaces_the_names_of_the_replay(hub: Path, tmp_path: Path) -> None:
    path = tmp_path / "names.txt"
    path.write_text("# neutral names\nalpha-svc=service-1\n", encoding="utf-8")
    document = replay(hub, rename_file=path)
    assert "alpha-svc" not in export.document_text(document)
    assert "renamed names" in document["run"]["redactions"]


def test_the_rename_option_is_parsed(hub: Path) -> None:
    arguments = export.build_parser().parse_args(
        [
            "--db",
            str(hub),
            "--project",
            PROJECT,
            "--rename",
            "alpha-svc=service-1",
            "--rename",
            "beta-svc=service-2",
        ]
    )
    assert export.options_of(arguments).renames == (
        ("alpha-svc", "service-1"),
        ("beta-svc", "service-2"),
    )


@pytest.mark.parametrize("value", ["alpha-svc", "=service-1", "alpha-svc="])
def test_a_malformed_rename_is_refused(hub: Path, value: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        export.main(["--db", str(hub), "--project", PROJECT, "--rename", value])
    assert stopped.value.code == 2


def test_a_bad_line_of_a_rename_file_is_reported(
    hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "names.txt"
    path.write_text("alpha-svc\n", encoding="utf-8")
    assert export.main(["--db", str(hub), "--project", PROJECT, "--rename-file", str(path)]) == 2
    assert "names.txt:1" in capsys.readouterr().err


def test_a_missing_rename_file_is_reported(
    hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "absent.txt"
    assert export.main(["--db", str(hub), "--project", PROJECT, "--rename-file", str(absent)]) == 2
    assert "absent.txt" in capsys.readouterr().err


# ----------------------------------------------------------------------------- transcripts
def test_the_answers_of_an_instance_become_messages(hub: Path, transcript: Path) -> None:
    document = replay(hub, transcripts=(("owner", transcript),))
    messages = of_type(document, "agent.message")
    assert [event["data"]["role"] for event in messages] == ["note", "final"]
    assert all(event["actor"] == "owner" for event in messages)
    assert all(event["source"] == "transcript" and event["seq"] is None for event in messages)
    assert messages[0]["data"]["text"] == NOTE.replace(address(10, 9, 8, 7), export.REDACTED)
    assert messages[1]["data"]["text"] == FINAL
    assert INSIDE_RUN not in export.document_text(document)


def test_a_long_message_is_cut_and_marked(hub: Path, transcript: Path) -> None:
    document = replay(hub, transcripts=(("owner", transcript),), message_chars=60)
    note, final = of_type(document, "agent.message")
    assert (note["data"]["truncated"], note["data"]["text"]) == (True, NOTE[:60])
    assert (final["data"]["truncated"], final["data"]["text"]) == (False, FINAL)


def test_a_message_that_two_sessions_hold_is_kept_once(hub: Path, tmp_path: Path) -> None:
    """A state database can hold the same message in two sessions; a real run showed it, and
    its replay told those answers twice."""
    first = [
        ("s1", "user", "make the delivery", unix(0)),
        ("s1", "assistant", NOTE, unix(2)),
        ("s1", "tool", "ok", unix(3)),
        ("s1", "assistant", FINAL, unix(5)),
    ]
    again = [("s2", role, text, moment) for _, role, text, moment in first]
    later = [
        ("s2", "user", "and now?", unix(7)),
        ("s2", "assistant", "OK.", unix(8)),
        ("s2", "user", "again?", unix(9)),
        ("s2", "assistant", "OK.", unix(10)),
    ]
    transcript = write_transcript(tmp_path / "state.db", first + again + later)
    messages = of_type(replay(hub, transcripts=(("owner", transcript),)), "agent.message")
    assert [(event["data"]["text"], event["data"]["role"]) for event in messages] == [
        (NOTE.replace(address(10, 9, 8, 7), export.REDACTED), "note"),
        (FINAL, "final"),
        ("OK.", "final"),
        ("OK.", "final"),
    ]


def test_both_sides_can_be_exported(hub: Path, tmp_path: Path) -> None:
    owner = build_transcript(tmp_path / "owner.db")
    build_transcript(tmp_path / "contractor.db")
    document = replay(
        hub, transcripts=(("owner", owner), ("contractor", tmp_path / "contractor.db"))
    )
    sides = {event["actor"] for event in of_type(document, "agent.message")}
    assert sides == {"owner", "contractor"}


# ----------------------------------------------------------------------------- ranges and kinds
def test_a_version_an_exported_record_names_comes_even_from_outside_the_window(
    tmp_path: Path,
) -> None:
    """The hub writes a version before the record that names it: the range may start after it.
    A version no exported record names, outside the window, stays out."""
    hub = HubFile(tmp_path / "hub.db", project())
    hub.version(1, OBJECTS, "operator")
    hub.clock.advance(1)
    second = hub.version(2, {**OBJECTS, "EXG-02": {**OBJECTS["EXG-01"], "id": "EXG-02"}}, OWNER)
    hub.log(10, "main-agent", Action.CONTRACT_APPROVE, {"change_id": "c_1", "version_id": second})
    hub.log(1, "hub", Action.TURN_END, {"side": "owner", "result": "completed"})
    hub.close()
    document = replay(tmp_path / "hub.db")
    versions = of_type(document, "contract.version")
    assert [event["data"]["number"] for event in versions] == [2]
    assert document["events"][0] == versions[0]
    assert document["contract_at_start"]["version"] == 1
    assert document["contract"]["version"] == 2


def test_a_range_keeps_only_its_records_and_its_window(hub: Path) -> None:
    document = replay(hub, from_seq=5, to_seq=10)
    assert document["run"]["from_seq"] == 5
    assert document["run"]["to_seq"] == 10
    assert document["integrity"]["records"] == 6
    sequences = [event["seq"] for event in document["events"] if event["seq"] is not None]
    assert sequences == [5, 6, 7, 8, 9, 10]
    assert of_type(document, "contract.version") == []
    assert of_type(document, "delivery.flag") == []


def test_only_a_scripted_run_names_attacks(hub: Path) -> None:
    scripted = replay(hub, kind="scripted", attacks=("ATK-91", "ATK-92"))
    assert scripted["run"]["kind"] == "scripted"
    assert scripted["run"]["attacks"] == ["ATK-91", "ATK-92"]
    assert replay(hub)["run"]["attacks"] == []
    assert replay(hub, kind="synthetic", attacks=("ATK-91",))["run"]["attacks"] == []


def test_the_run_carries_its_title_model_and_stream(hub: Path) -> None:
    run = replay(hub, model="deepseek-v4.1-flash")["run"]
    assert run["title"] == "a synthetic run"
    assert run["model"] == "deepseek-v4.1-flash"
    assert run["stream"] == PROJECT
    assert run["project"]["name"] == "alpha-svc"
    assert run["exported_at"] == EXPORTED_AT
    assert run["exporter"] == f"tools.replay.export {export.VERSION}"
    assert run["started_at"] == replay(hub)["events"][0]["t"]


def test_two_exports_of_the_same_range_are_identical(hub: Path) -> None:
    assert export.document_text(replay(hub)) == export.document_text(replay(hub))


def test_a_run_without_a_model_call_still_totals() -> None:
    totals = export.totals_of([], START, START)
    assert totals["cost_usd"] == {"total": 0.0}
    assert totals["model_calls"] == {}
    assert totals["decisions"] == []


def test_the_offsets_start_at_zero_and_never_go_back(hub: Path) -> None:
    offsets = [event["offset_ms"] for event in replay(hub)["events"]]
    assert offsets[0] == 0
    assert offsets == sorted(offsets)


# ----------------------------------------------------------------------------- the player page
PLAYER = '<html><body><script type="application/json" id="replay-data"></script></body></html>'


def embedded(html: str) -> str:
    start = html.index(">", html.index('id="replay-data"')) + 1
    return html[start : html.index("</script>", start)]


def test_the_player_carries_the_replay_as_json(hub: Path, transcript: Path) -> None:
    document = replay(hub, transcripts=(("owner", transcript),))
    html = export.player_document(PLAYER, export.document_text(document))
    assert json.loads(embedded(html)) == document
    assert html.count("</script>") == 1


@pytest.mark.parametrize("text", ["</script>", "<!--", "<script>", "</SCRIPT >", "a < b"])
def test_no_text_of_the_replay_can_end_or_hide_the_element(text: str) -> None:
    payload = json.dumps({"text": text})
    inside = embedded(export.player_document(PLAYER, payload))
    assert "<" not in inside
    assert json.loads(inside) == {"text": text}


def test_a_template_without_the_element_is_refused() -> None:
    with pytest.raises(export.ReplayError, match="exactly one"):
        export.player_document("<html></html>", "{}")


def test_the_player_template_must_exist(
    hub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "PLAYER_TEMPLATE", tmp_path / "absent.html")
    assert (
        export.main(
            ["--db", str(hub), "--project", PROJECT, "--html", str(tmp_path / "player.html")]
        )
        == 2
    )


def test_the_command_line_writes_the_player_with_the_replay(hub: Path, tmp_path: Path) -> None:
    page = tmp_path / "player.html"
    assert export.main(["--db", str(hub), "--project", PROJECT, "--html", str(page)]) == 0
    assert json.loads(embedded(page.read_text(encoding="utf-8")))["format"] == "zephyros-replay"


# ----------------------------------------------------------------------------- the command line
def test_the_command_line_writes_the_output_file(hub: Path, tmp_path: Path) -> None:
    output = tmp_path / "run.replay.json"
    code = export.main(
        ["--db", str(hub), "--project", PROJECT, "--title", "a title", "--output", str(output)]
    )
    assert code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["run"]["title"] == "a title"
    assert written["format"] == "zephyros-replay"
    assert written["integrity"]["chain"] == "verified"


def test_the_command_line_prints_to_stdout_by_default(
    hub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert export.main(["--db", str(hub), "--project", PROJECT]) == 0
    assert '"format": "zephyros-replay"' in capsys.readouterr().out


def test_an_unknown_project_is_reported(hub: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert export.main(["--db", str(hub), "--project", "p_missing"]) == 2
    assert "p_missing" in capsys.readouterr().err


def test_a_database_that_cannot_be_read_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "absent.db"
    assert export.main(["--db", str(absent), "--project", PROJECT]) == 2
    assert f"cannot read {absent}" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--output", "--html"])
def test_a_file_that_cannot_be_written_is_named(
    hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], option: str
) -> None:
    target = tmp_path / "no such directory" / "run.out"
    assert export.main(["--db", str(hub), "--project", PROJECT, option, str(target)]) == 2
    err = capsys.readouterr().err
    assert f"cannot write {target}" in err
    assert "cannot read" not in err


def test_a_transcript_that_cannot_be_read_is_named(
    hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "absent-state.db"
    arguments = ["--db", str(hub), "--project", PROJECT, "--transcript", f"owner={absent}"]
    assert export.main(arguments) == 2
    err = capsys.readouterr().err
    assert f"--transcript {absent}" in err
    assert f"cannot read {hub}" not in err


def test_an_unusable_redaction_pattern_is_reported(
    hub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert export.main(["--db", str(hub), "--project", PROJECT, "--redact", "["]) == 2
    assert "--redact" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["mallory=/tmp/x.db", "owner", "owner="])
def test_a_bad_transcript_option_is_refused(hub: Path, value: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        export.main(["--db", str(hub), "--project", PROJECT, "--transcript", value])
    assert stopped.value.code == 2


def test_the_default_protected_path_is_the_contract_file(hub: Path) -> None:
    arguments = export.build_parser().parse_args(["--db", str(hub), "--project", PROJECT])
    assert export.options_of(arguments).protected_paths == ("CONTRACT.md",)
    overridden = export.build_parser().parse_args(
        ["--db", str(hub), "--project", PROJECT, "--protected-path", "acceptance/"]
    )
    assert export.options_of(overridden).protected_paths == ("acceptance/",)


def test_the_transcript_option_is_parsed(hub: Path) -> None:
    arguments = export.build_parser().parse_args(
        ["--db", str(hub), "--project", PROJECT, "--transcript", "contractor=state.db"]
    )
    assert export.options_of(arguments).transcripts == (("contractor", Path("state.db")),)


# ----------------------------------------------------------------------------- the example
def test_the_example_is_what_the_exporter_makes_of_its_run(tmp_path: Path) -> None:
    """The example is never edited by hand. After a change of the exporter or of its run:
    `uv run --locked python -m tests.unit.tools.replay_example` writes it again."""
    assert EXAMPLE.read_text(encoding="utf-8") == example_text(tmp_path)


def test_the_example_says_it_is_synthetic_and_verifies() -> None:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert document["version"] == export.VERSION
    assert document["run"]["kind"] == "synthetic"
    assert document["run"]["attacks"] == []
    assert document["integrity"]["chain"] == "verified"
    assert [event["id"] for event in document["events"]] == list(range(len(document["events"])))


# ----------------------------------------------------------------------------- the format page
FORMAT_PAGE = Path(__file__).parents[3] / "docs" / "replay-format.md"


def test_the_format_page_names_the_version_the_exporter_writes() -> None:
    assert f"Current version: **{export.VERSION}**." in FORMAT_PAGE.read_text(encoding="utf-8")


def test_the_format_page_documents_every_option_of_the_command_line() -> None:
    text = FORMAT_PAGE.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `(--[a-z-]+)", text, re.MULTILINE))
    options = {
        option
        for action in export.build_parser()._actions  # reason: argparse lists them nowhere else
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }
    assert documented == options
