"""The replay player, run in a real headless browser.

The page under test is the one the exporter writes with `--html`: the player with a replay embedded
in it. Each test opens it in Chromium and drives it through `window.replay`, the interface the
recorder uses. Any uncaught exception of the page, and any error it logs, fails the test: the player
logs an event it cannot show and goes on, so a test that only watched the playback would miss it.

A machine with neither Chromium nor Chrome skips these tests, except on a CI (`CI` set), where they
fail: there, a missing browser must not pass for a passing player. `CHROMIUM` names the browser.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tools.replay import export

from tests.unit.tools.headless_chromium import Chromium, find_chromium
from tests.unit.tools.replay_example import EXAMPLE, FIRST, SECOND
from tests.unit.tools.replay_hub import HubFile
from zephyros.core.audit import Action, Outcome
from zephyros.core.deliveries import DeliveryRecord, DeliveryState
from zephyros.core.projects import Project

BROWSER = find_chromium()

TIMES = "\N{MULTIPLICATION SIGN}"
STATE = "window.replay.state()"
DONE = "window.replay.state().done"


@pytest.fixture(scope="module")
def browser() -> Iterator[Chromium]:
    if BROWSER is None:
        if os.environ.get("CI"):
            pytest.fail("no Chromium or Chrome on this CI: the player tests must run here")
        pytest.skip("not applicable here: no Chromium or Chrome found (set CHROMIUM)")
    with Chromium(BROWSER) as running:
        yield running


@pytest.fixture
def chromium(browser: Chromium) -> Chromium:
    """The browser, with no error of the page left over from another test."""
    browser.errors.clear()
    return browser


def example() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return document


def page_of(directory: Path, document: dict[str, Any] | str) -> Path:
    """The player with `document` embedded, as `--html` writes it."""
    payload = document if isinstance(document, str) else export.document_text(document)
    page = directory / "player.html"
    template = export.PLAYER_TEMPLATE.read_text(encoding="utf-8")
    page.write_text(export.player_document(template, payload), encoding="utf-8")
    return page


def play_to_the_end(chromium: Chromium) -> dict[str, Any]:
    chromium.evaluate("window.replay.setSpeed(1000); window.replay.play()")
    chromium.poll(DONE, timeout=20)
    state: dict[str, Any] = chromium.evaluate(STATE)
    return state


def text(chromium: Chromium, selector: str) -> str:
    return str(chromium.evaluate(f"document.querySelector({json.dumps(selector)}).textContent"))


def texts(chromium: Chromium, selector: str) -> list[str]:
    found = chromium.evaluate(
        f"Array.from(document.querySelectorAll({json.dumps(selector)}), n => n.textContent)"
    )
    return [str(item) for item in found]


def ids_of(document: dict[str, Any], **fields: str) -> list[int]:
    return [
        int(event["id"])
        for event in document["events"]
        if all(event.get(key) == value for key, value in fields.items())
    ]


# ----------------------------------------------------------------------------- a whole run
def test_the_example_plays_to_its_end(chromium: Chromium, tmp_path: Path) -> None:
    document = example()
    chromium.open(page_of(tmp_path, document))
    state = play_to_the_end(chromium)
    assert state == {"eventId": document["events"][-1]["id"], "playing": False, "done": True}
    assert chromium.errors == []
    assert texts(chromium, "#feed .fs")[-1] == document["events"][-1]["summary"]
    assert text(chromium, "#gate-big") == "PASS"
    assert text(chromium, "#gate-tests") == "2/2 acceptance tests passed"
    assert text(chromium, "#verdict-badge") == "accept with reservations"
    assert text(chromium, "#verdict-reasons").startswith("Both tests pass")
    assert texts(chromium, "#flags-list .fp") == ["CONTRACT.md"]
    assert text(chromium, "#kind-badge") == "SYNTHETIC · test data, not a run"
    assert text(chromium, "#integrity").startswith("hash chain consistent")


def test_the_refusals_and_the_errors_of_every_actor_are_shown(
    chromium: Chromium, tmp_path: Path
) -> None:
    """A refusal of the hub or of the operator has no lane: it once stopped the player."""
    document = example()
    refused = [event for event in document["events"] if event["outcome"] == "refused"]
    failed = [event for event in document["events"] if event["outcome"] == "error"]
    assert {event["actor"] for event in refused} == {"hub", "operator", "contractor"}
    assert {event["actor"] for event in failed} == {"hub", "contractor"}
    chromium.open(page_of(tmp_path, document))
    play_to_the_end(chromium)
    assert chromium.errors == []
    assert texts(chromium, "#feed .fs.refused") == [event["summary"] for event in refused]
    assert texts(chromium, "#feed .fs.error") == [event["summary"] for event in failed]
    counters = text(chromium, "#cnt-contractor")
    assert f"refused {TIMES}1" in counters
    assert f"errors {TIMES}1" in counters
    assert texts(chromium, "#gate-history .d") == ["d1", "d2", "d2"]
    assert texts(chromium, "#gate-history .mini") == ["REFUSED", "CONCERNS", "PASS"]


def test_every_event_can_be_reached_forward_and_backward(
    chromium: Chromium, tmp_path: Path
) -> None:
    document = example()
    chromium.open(page_of(tmp_path, document))
    identifiers = [event["id"] for event in document["events"]]
    reached = chromium.evaluate(
        f"{json.dumps(identifiers)}.map(id => (window.replay.focus(id), {STATE}.eventId))"
    )
    assert reached == identifiers
    backward = list(reversed(identifiers))
    reached = chromium.evaluate(
        f"{json.dumps(backward)}.map(id => (window.replay.seek(id), {STATE}.eventId))"
    )
    assert reached == backward
    assert chromium.errors == []


@pytest.mark.parametrize("actor", ["hub", "operator"])
def test_a_link_to_a_refusal_opens_on_it(chromium: Chromium, tmp_path: Path, actor: str) -> None:
    document = example()
    [refusal] = ids_of(document, actor=actor, outcome="refused")
    chromium.open(page_of(tmp_path, document), f"from={refusal}")
    assert chromium.evaluate("document.getElementById('fatal').hidden") is True
    assert chromium.evaluate(STATE)["eventId"] == refusal
    assert play_to_the_end(chromium)["eventId"] == document["events"][-1]["id"]
    assert chromium.errors == []


# ----------------------------------------------------------------------------- the contract
def test_the_contract_panel_follows_the_versions(chromium: Chromium, tmp_path: Path) -> None:
    document = example()
    first, second = ids_of(document, type="contract.version")
    chromium.open(page_of(tmp_path, document))
    criteria = "Array.from(document.querySelectorAll('#contract-list .cid'), n => n.textContent)"

    chromium.evaluate("window.replay.seek(0)")
    assert text(chromium, "#contract-version") == "—"
    assert text(chromium, "#contract-list") == "No contract yet."

    chromium.evaluate(f"window.replay.seek({first})")
    assert text(chromium, "#contract-version") == "v1"
    assert "US-E1-02.1" not in chromium.evaluate(criteria)

    chromium.evaluate(f"window.replay.seek({second})")
    assert text(chromium, "#contract-version") == "v2"
    assert "US-E1-02.1" in chromium.evaluate(criteria)
    assert texts(chromium, "#contract-list .citem.changed .cid") == ["US-E1-02.1"]

    chromium.evaluate(f"window.replay.seek({second - 1})")
    assert text(chromium, "#contract-version") == "v1"
    assert chromium.errors == []


PROJECT = "p_0f1e2d3c"
OWNER = f"owner:{PROJECT}"
OBSERVATION = "standard library only"
MARK = "document.querySelector('#contract-list .citem[data-id=\"CTR-01\"] .mk')"


def inspected_then_amended(directory: Path) -> dict[str, Any]:
    """A run where the Owner inspects CTR-01 before a new version of the contract; then the hub
    cannot send an alert about the Owner, and a model call of the Owner fails."""
    path = directory / "hub.db"
    start = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    hub = HubFile(path, Project(PROJECT, "status-svc", None, start, "operator"))
    first = hub.version(1, FIRST, "operator")
    hub.log(0, "operator", Action.CONTRACT_IMPORT, {"version_id": first, "objects": len(FIRST)})
    inspected = hub.log(
        5,
        OWNER,
        Action.INSPECTION_RECORD,
        {"delivery_id": "d1", "object_id": "CTR-01", "verdict": "conforms", "decision": "PASS"},
    )
    hub.clock.advance(5)
    second = hub.version(2, SECOND, "main-agent")
    hub.log(
        0,
        "main-agent",
        Action.CONTRACT_APPROVE,
        {"change_id": "c_0a1b2c3d", "approver": "main-agent", "version_id": second},
    )
    alert = {"side": "owner", "step": "alert_human", "task": "decision", "error": "status 500"}
    hub.log(5, "hub", Action.NOTIFICATION, alert, Outcome.ERROR)
    hub.log(5, OWNER, Action.GATEWAY_CALL, {"model": "m", "detail": "HTTP 503"}, Outcome.ERROR)
    stored = {"verdict": "conforms", "observation": OBSERVATION, "by": OWNER, "at": inspected}
    hub.delivery(
        DeliveryRecord(
            PROJECT,
            "d1",
            "feature",
            DeliveryState.PROPOSED,
            inspected,
            decision="PASS",
            contract_version=1,
            inspections={"CTR-01": stored},
        )
    )
    hub.close()
    options = export.Options(db=path, project=PROJECT, kind="synthetic")
    return export.replay_document(options, exported_at="2026-09-21T15:00:00.000000Z")


def test_an_inspection_mark_outlives_a_new_version_of_the_contract(
    chromium: Chromium, tmp_path: Path
) -> None:
    document = inspected_then_amended(tmp_path)
    [inspection] = ids_of(document, type="inspection.record")
    first, second = ids_of(document, type="contract.version")
    assert first < inspection < second
    chromium.open(page_of(tmp_path, document))

    chromium.evaluate(f"window.replay.seek({inspection})")
    assert chromium.evaluate(f"{MARK}.textContent") == "✓ conforms"

    chromium.evaluate(f"window.replay.seek({second})")
    assert text(chromium, "#contract-version") == "v2"
    assert chromium.evaluate(f"{MARK}.hidden") is False
    assert chromium.evaluate(f"{MARK}.textContent") == "✓ conforms"
    assert chromium.evaluate(f"{MARK}.title") == OBSERVATION

    chromium.evaluate(f"window.replay.seek({first})")
    assert chromium.evaluate(f"{MARK}.hidden") is True
    assert chromium.errors == []


def test_an_error_counts_for_the_side_that_failed_only(chromium: Chromium, tmp_path: Path) -> None:
    """The hub names the Owner in an alert it could not send: that is no error of the Owner."""
    document = inspected_then_amended(tmp_path)
    failed = [event for event in document["events"] if event["outcome"] == "error"]
    assert [(event["actor"], event["data"].get("side")) for event in failed] == [
        ("hub", "owner"),
        ("owner", "owner"),
    ]
    chromium.open(page_of(tmp_path, document))
    play_to_the_end(chromium)
    assert texts(chromium, "#feed .fs.error") == [event["summary"] for event in failed]
    assert f"errors {TIMES}1" in text(chromium, "#cnt-owner")
    assert texts(chromium, "#bub-owner .bubble.error .btag") == ["error · gateway.call"]
    assert chromium.errors == []


# ----------------------------------------------------------------------------- what can go wrong
def test_a_replay_with_strange_events_plays_to_its_end(chromium: Chromium, tmp_path: Path) -> None:
    """Actors without a lane, unknown kinds and categories, missing or odd fields: the player
    shows what it can, and every event is still played."""
    events: list[Any] = [
        {
            "actor": "main-agent",
            "type": "contract.approve",
            "category": "refusal",
            "outcome": "refused",
            "summary": "Refused (self_approval): no",
            "data": {"reason": "x"},
        },
        {
            "actor": "hub",
            "type": "egress.refused",
            "category": "refusal",
            "outcome": "refused",
            "data": {"host": "hooks.example.invalid", "purpose": "notification"},
        },
        {
            "actor": "hub",
            "type": "delivery.check",
            "category": "refusal",
            "outcome": "refused",
            "data": {"delivery_id": "d9", "reason": "protected_path"},
        },
        {
            "actor": "operator",
            "type": "contract.read",
            "category": "refusal",
            "outcome": "refused",
            "data": None,
        },
        {
            "actor": "stranger",
            "type": "turn.start",
            "category": "turn",
            "outcome": "error",
            "data": {"side": "owner:p_1", "error": "no"},
        },
        {"actor": "hub", "type": "agent.message", "category": "message", "data": {"text": 7}},
        {
            "actor": "owner",
            "type": "gateway.call",
            "category": "model",
            "outcome": "error",
            "data": {"side": "owner", "detail": "HTTP 503"},
        },
        {"actor": "contractor", "type": "x.y", "category": "no-such-category", "summary": 12},
        {
            "actor": "hub",
            "type": "gate.decision",
            "category": "gate",
            "t": 5,
            "data": {"decision": "MAYBE"},
            "hub_state": {"outcomes": {"a": "passed", "b": 3}},
        },
        {
            "actor": "owner",
            "type": "delivery.review",
            "category": "judgment",
            "data": {"verdict": 4},
            "hub_state": None,
        },
        {
            "actor": "hub",
            "type": "delivery.check",
            "category": "flag",
            "data": {},
            "hub_state": {"paths": ["CONTRACT.md", None]},
        },
        {
            "actor": "hub",
            "type": "contract.version",
            "category": "contract",
            "data": {"number": 3, "objects": [None, {"id": "EXG-01"}, {"text": "no id"}]},
        },
        None,
        "not an event",
        {
            "actor": "owner",
            "type": "agent.message",
            "category": "message",
            "data": {
                "side": "owner",
                "role": "final",
                "text": "</script><script>window.planted = 1</script><!-- <script>",
            },
        },
    ]
    for offset, event in enumerate(events):
        if isinstance(event, dict):
            event["offset_ms"] = offset * 1000
            event.setdefault("t", f"2026-01-01T00:00:{offset:02d}.000000Z")
    document = {
        "format": "zephyros-replay",
        "version": export.VERSION,
        "run": {"kind": "no-such-kind", "title": None, "project": "p_1"},
        "integrity": "verified",
        "contract_at_start": {"version": 2, "objects": "none"},
        "events": events,
        "totals": None,
    }
    chromium.open(page_of(tmp_path, document))
    played = [event for event in events if isinstance(event, dict)]
    assert play_to_the_end(chromium)["eventId"] == len(played) - 1
    assert chromium.errors == []
    assert chromium.evaluate("typeof window.planted") == "undefined"
    assert text(chromium, "#kind-badge") == "KIND · no-such-kind"
    assert "</script>" in texts(chromium, "#bub-owner .bubble.final")[-1]


def test_a_replay_of_another_major_version_is_refused(chromium: Chromium, tmp_path: Path) -> None:
    document = {**example(), "version": "1.0.0"}
    chromium.open(page_of(tmp_path, document))
    assert chromium.evaluate("document.getElementById('fatal').hidden") is False
    assert "major version 2" in text(chromium, "#fatal-msg")


def test_an_embedded_text_that_is_not_json_is_said_so(chromium: Chromium, tmp_path: Path) -> None:
    chromium.open(page_of(tmp_path, '{"format": "zephyros-replay",'))
    assert text(chromium, "#fatal-msg").startswith("The embedded replay is not valid JSON")
