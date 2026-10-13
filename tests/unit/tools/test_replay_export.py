"""The replay exporter (docs/replay-format.md): what it keeps, drops, derives and totals.

The hub database is built with the project's own SQLite adapters and `AuditLog`, so the hashes
and the chain in the tests are the real ones.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tools.replay import export

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.sqlite.contract_store import SqliteContractStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.adapters.sqlite.log_store import SqliteLogStore
from zephyros.adapters.sqlite.project_store import SqliteProjectStore
from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.clock import parse_time
from zephyros.core.contract import ContractVersion, content_hash, version_id
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
        Action.INSPECTION_RECORD,
        Outcome.OK,
        OWNER,
        Reason.NONE,
        {
            "decision": "CONCERNS",
            "delivery_id": "d1",
            "object_id": "CTR-01",
            "verdict": "conforms",
            "source": OWNER_ADDRESS,
        },
    ),
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
PUSH_TIME = 11  # the record that carried the delivery, one second per record from 09:00:00


def unix(second: int) -> float:
    return datetime(2026, 3, 4, 9, 0, second, tzinfo=UTC).timestamp()


def build_hub(tmp_path: Path) -> Path:
    """A small hub database, where every record really was hashed by the hub's own log."""
    path = tmp_path / "hub.db"
    database = Database(path)
    clock = FakeClock(START)
    audit = AuditLog(SqliteLogStore(database), clock)
    SqliteProjectStore(database).add(
        Project(
            id=PROJECT,
            name="alpha-svc",
            repository_url="https://example.invalid/alpha-svc",
            created_at=clock.now(),
            created_by="operator",
        )
    )
    contracts = SqliteContractStore(database)
    times: list[str] = []
    for action, outcome, principal, reason, parameters in RECORDS:
        times.append(
            audit.append(AuditEvent(PROJECT, principal, action, outcome, reason, parameters)).time
        )
        clock.advance(1)
    digest = content_hash(OBJECTS)
    contracts.append(
        PROJECT,
        ContractVersion(
            number=1,
            id=version_id(1, digest),
            parent=None,
            content_hash=digest,
            objects=OBJECTS,
            author="operator",
            time=parse_time(times[1]),
            operator_written=True,
        ),
    )
    SqliteDeliveryStore(database).put_record(
        DeliveryRecord(
            project_id=PROJECT,
            delivery_id="d1",
            branch="feature",
            state=DeliveryState.PROPOSED,
            received_at=times[PUSH_TIME],
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
                    "at": times[16],
                }
            },
            review={
                "verdict": "accept_with_reservations",
                "reasons": f"all accepted; the probe came from {address(100, 64, 12, 7)}",
                "by": OWNER,
                "at": times[18],
            },
        )
    )
    database.close()
    return path


def build_transcript(path: Path) -> Path:
    """A tiny Hermes state database: the Owner's conversation, with two answers in the run."""
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)"
        )
        rows = [
            ("s1", "user", "make the delivery", unix(0)),
            ("s1", "assistant", NOTE, unix(2)),
            ("s1", "tool", "ok", unix(3)),
            ("s1", "assistant", "", unix(4)),
            ("s1", "assistant", FINAL, unix(5)),
            ("s1", "user", "thank you", unix(6)),
            ("s1", "assistant", INSIDE_RUN, unix(0) - 3600),
            ("s1", "assistant", INSIDE_RUN, unix(0) + 3600),
        ]
        db.executemany(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp) "
            "VALUES (?, ?, ?, NULL, NULL, ?)",
            rows,
        )
    return path


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
    with sqlite3.connect(path) as db:
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
    assert document["totals"]["model_calls"] == {"contractor": 1, "owner": 3}
    assert document["totals"]["tokens"] == {
        "contractor": {"input": 500, "output": 250},
        "owner": {"input": 3000, "output": 600},
    }
    assert document["totals"]["cost_usd"] == {
        "contractor": 0.000225,
        "owner": 0.00081,
        "total": 0.001035,
    }
    assert document["totals"]["refusals"] == {"contractor": 0, "owner": 1}


def test_the_pricing_comes_from_the_command_line(hub: Path) -> None:
    document = replay(hub, input_price=3.0, output_price=15.0, price_note="a price list")
    assert document["pricing"] == {
        "input_per_million": 3.0,
        "output_per_million": 15.0,
        "currency": "USD",
        "note": "a price list",
    }
    assert document["totals"]["cost_usd"]["owner"] == 0.018


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


# ----------------------------------------------------------------------------- hub-state events
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
    }
    assert objects["US-E1-01.1"]["verification"] == "T"
    assert objects["US-E1-01.1"]["priority"] is None


def test_the_contract_is_the_one_in_force_at_the_end(hub: Path) -> None:
    assert replay(hub)["contract"]["version"] == 1
    assert replay(hub)["contract"]["version_id"] == version_id(1, content_hash(OBJECTS))
    assert len(replay(hub)["contract"]["objects"]) == len(OBJECTS)


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


def test_a_flag_the_hub_announced_names_the_hub() -> None:
    body: dict[str, Any] = {
        "received_at": "2026-03-04T09:00:11.000000Z",
        "flagged_paths": ["CONTRACT.md"],
    }
    event = export.flagged_event("d1", body, ("CONTRACT.md",))
    assert event is not None
    assert event.data["by"] == "hub"


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


def test_the_delivery_details_are_enriched_from_the_hub_tables(hub: Path) -> None:
    document = replay(hub)
    push = only(document, "delivery.push")
    assert push["data"]["branch"] == "feature"
    assert push["data"]["changed_paths"] == ["alpha_svc/app.py", "CONTRACT.md"]
    gate = of_type(document, "gate.decision")[0]
    assert gate["data"]["outcomes"] == {
        "test_x.py::test_a": "passed",
        "test_x.py::test_b": "passed",
    }
    assert gate["summary"] == "Gate: CONCERNS for d1 (contract v1)"
    inspection = only(document, "inspection.record")
    assert inspection["data"]["observation"] == "stdlib only, checked on [redacted]"
    assert inspection["category"] == "judgment"
    review = only(document, "delivery.review")
    assert review["data"]["reasons"] == "all accepted; the probe came from [redacted]"
    assert review["data"]["verdict"] == "accept_with_reservations"
    opened = only(document, "pull_request.open")
    assert opened["data"]["number"] == 7
    assert opened["data"]["url"] == "https://example.invalid/alpha-svc/pull/7"


# ----------------------------------------------------------------------------- redaction
def test_private_addresses_are_replaced_and_listed(hub: Path) -> None:
    document = replay(hub)
    text = export.document_text(document)
    assert address(192, 168, 1, 10) not in text
    assert address(10, 0, 0, 5) not in text
    assert export.REDACTED in text
    assert document["run"]["redactions"] == ["private IPv4 addresses"]


def test_a_pattern_of_the_command_line_is_replaced_too(hub: Path) -> None:
    document = replay(hub, redact=("no such path",))
    assert document["run"]["redactions"] == ["pattern no such path", "private IPv4 addresses"]
    assert only(document, "unknown")["summary"] == "Refused (not_found): [redacted] [redacted]"


def test_the_carrier_grade_range_is_private_too() -> None:
    redactor = export.Redactor.of(())
    below, above = address(100, 63, 0, 1), address(100, 128, 0, 1)
    inside = " ".join([below, address(100, 64, 0, 1), address(100, 127, 255, 254), above])
    assert redactor.text(inside) == " ".join([below, export.REDACTED, export.REDACTED, above])
    assert redactor.replaced == {"private IPv4 addresses"}


# ----------------------------------------------------------------------------- renaming
def test_a_name_is_replaced_by_a_neutral_one(hub: Path) -> None:
    document = replay(hub, renames=(("alpha-svc", "service-1"), ("feature", "branch-1")))
    text = export.document_text(document)
    assert "alpha-svc" not in text
    assert "feature" not in text
    assert document["run"]["project"]["name"] == "service-1"
    assert (
        only(document, "delivery.push")["summary"] == "Contractor pushes d1 on branch-1 (2 paths)"
    )
    assert only(document, "delivery.push")["data"]["branch"] == "branch-1"
    assert "renamed names" in document["run"]["redactions"]
    assert all("alpha-svc" not in entry for entry in document["run"]["redactions"])


def test_a_rename_is_case_insensitive_and_matches_whole_words() -> None:
    redactor = export.Redactor.of((), (("alpha-svc", "service-1"),))
    assert redactor.text("Alpha-Svc, ALPHA-SVC and alpha-svc") == (
        "service-1, service-1 and service-1"
    )
    assert redactor.text("myalpha-svc") == "myalpha-svc"
    assert redactor.replaced == {"renamed names"}


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


def test_both_sides_can_be_exported(hub: Path, tmp_path: Path) -> None:
    owner = build_transcript(tmp_path / "owner.db")
    build_transcript(tmp_path / "contractor.db")
    document = replay(
        hub, transcripts=(("owner", owner), ("contractor", tmp_path / "contractor.db"))
    )
    sides = {event["actor"] for event in of_type(document, "agent.message")}
    assert sides == {"owner", "contractor"}


# ----------------------------------------------------------------------------- ranges and kinds
def test_a_range_keeps_only_its_records_and_its_window(hub: Path) -> None:
    document = replay(hub, from_seq=5, to_seq=10)
    assert document["run"]["from_seq"] == 5
    assert document["run"]["to_seq"] == 10
    assert document["integrity"]["records"] == 6
    sequences = [event["seq"] for event in document["events"] if event["seq"] is not None]
    assert sequences == [5, 6, 7, 8, 9, 10]
    assert of_type(document, "contract.version") == []
    assert of_type(document, "delivery.flag") == []


def test_a_scripted_run_names_its_attacks_and_a_real_run_has_none(hub: Path) -> None:
    scripted = replay(hub, kind="scripted", attacks=("ATK-91", "ATK-92"))
    assert scripted["run"]["kind"] == "scripted"
    assert scripted["run"]["attacks"] == ["ATK-91", "ATK-92"]
    assert replay(hub)["run"]["attacks"] == []


def test_the_run_carries_its_title_model_and_stream(hub: Path) -> None:
    run = replay(hub, model="deepseek-v4.1-flash")["run"]
    assert run["title"] == "a synthetic run"
    assert run["model"] == "deepseek-v4.1-flash"
    assert run["stream"] == PROJECT
    assert run["project"]["name"] == "alpha-svc"
    assert run["exported_at"] == EXPORTED_AT
    assert run["exporter"] == "tools.replay.export 1.0.0"
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


# ----------------------------------------------------------------------------- the player
PLAYER = '<html><body><script type="application/json" id="replay-data"></script></body></html>'


def test_the_player_carries_the_replay_as_json(hub: Path, transcript: Path) -> None:
    document = replay(hub, transcripts=(("owner", transcript),))
    html = export.player_document(PLAYER, export.document_text(document))
    start = html.index('id="replay-data"')
    start = html.index(">", start) + 1
    end = html.index("</script>", start)
    assert json.loads(html[start:end].replace("<\\/", "</")) == document
    assert html.count("</script>") == 1


def test_the_end_of_a_script_tag_inside_a_message_is_escaped() -> None:
    html = export.player_document(PLAYER, '{"text": "</script>"}')
    assert "<\\/script>" in html
    assert html.count("</script>") == 1


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
def example_document() -> dict[str, Any]:
    """The synthetic example the repository ships, next to the exporter."""
    path = Path(export.__file__).with_name("example.replay.json")
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def test_the_example_is_a_replay_document_the_player_accepts() -> None:
    document = example_document()
    assert document["format"] == "zephyros-replay"
    assert document["version"].startswith("1.")
    assert document["run"]["kind"] == "scripted"
    assert [event["id"] for event in document["events"]] == list(range(len(document["events"])))


def test_the_example_names_the_objects_of_its_contract_version_in_full() -> None:
    events = example_document()["events"]
    version = next(event for event in events if event["type"] == "contract.version")
    assert version["source"] == "hub-state"
    objects = version["data"]["objects"]
    assert objects, "the version must carry its objects, not bare identifiers"
    assert all(
        set(item) == {"id", "type", "title", "text", "priority", "verification"} for item in objects
    )
