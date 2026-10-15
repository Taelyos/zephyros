"""ADR 0030: the tools of a project at the gate. The hub measures them before and after each
verification, names them in the log and in the evidence report, flags the turns that found
other tools, and keeps no result reached with tools that changed or that it refuses."""

from __future__ import annotations

import json

from tests.unit.core.test_deliveries import BASE, delivered, ready, service
from tests.unit.core.test_lifecycle import FIRST_TOOLS, OTHER_TOOLS, ScriptedTools
from zephyros.adapters.dummy.delivery import make_package, state_id
from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome
from zephyros.core.deliveries import DeliveryState
from zephyros.core.tools import ToolsError


def runs(audit: AuditLog, project: str) -> list[dict[str, object]]:
    return [
        json.loads(r.parameters)
        for r in audit.read(project)
        if (r.action, r.outcome) == (Action.VERIFICATION_RUN, Outcome.OK)
    ]


def test_the_fingerprint_of_the_tools_is_in_the_log_and_in_the_report() -> None:
    made, project, audit, _store = service(tools=ScriptedTools(FIRST_TOOLS))
    ready(made, project)
    found = made.status(project, delivered(made, project))
    assert found.state is DeliveryState.PROPOSED
    assert runs(audit, project)[0]["tools"] == FIRST_TOOLS.fingerprint
    assert found.report is not None
    assert (
        f"- Project tools: `{FIRST_TOOLS.fingerprint}` (2 files, 300 bytes), mounted read-only "
        "at `/opt/project-tools` in the turns of both sides and in the sandbox"
    ) in found.report.splitlines()
    contractor = found.contractor_report
    assert contractor is not None
    assert FIRST_TOOLS.fingerprint in contractor


def test_without_tools_neither_the_log_nor_the_report_names_any() -> None:
    made, project, audit, _store = service()
    ready(made, project)
    found = made.status(project, delivered(made, project))
    assert "tools" not in runs(audit, project)[0]
    assert found.report is not None
    assert "Project tools" not in found.report


def test_tools_that_change_during_the_run_drop_its_results() -> None:
    tools = ScriptedTools(FIRST_TOOLS, OTHER_TOOLS)
    made, project, audit, _store = service(tools=tools)
    ready(made, project)
    found = made.status(project, delivered(made, project))
    assert tools.asked == 2  # before the run and after it
    assert found.state is DeliveryState.ERROR
    assert found.decision is None
    assert found.detail == "the tools changed during the verification: its results are not kept"
    errors = [r for r in audit.read(project) if r.action is Action.VERIFICATION_RUN]
    assert [r.outcome for r in errors] == [Outcome.ERROR]


def test_tools_the_hub_refuses_run_no_verification() -> None:
    refused = ToolsError(["'bin' is writable by every user", "'lib/x' is a link that leads out"])
    made, project, _audit, _store = service(tools=ScriptedTools(refused))
    ready(made, project)
    found = made.status(project, delivered(made, project))
    assert found.state is DeliveryState.ERROR
    assert found.detail == (
        "the tools cannot be mounted: 'bin' is writable by every user; "
        "'lib/x' is a link that leads out"
    )


def test_the_report_flags_the_turns_that_found_other_tools_before_the_delivery() -> None:
    """Read from the starts of turn of both sides, up to the push of the delivery: a turn after
    the push did not build what the gate judges."""
    made, project, audit, _store = service(tools=ScriptedTools(FIRST_TOOLS))
    ready(made, project)

    def turn(side: str, **tools: str) -> None:
        audit.append(
            AuditEvent(
                project, "hub", Action.TURN_START, Outcome.OK, parameters={"side": side, **tools}
            )
        )

    turn("owner")  # before the tools were configured
    turn("owner", tools=FIRST_TOOLS.fingerprint)
    turn("contractor", tools=OTHER_TOOLS.fingerprint)
    turn("contractor", tools=FIRST_TOOLS.fingerprint)
    record = made.push(project, make_package({**BASE, "calc.py": b"x = 2\n"}, state_id(BASE)), "w")
    audit.append(
        AuditEvent(
            project,
            f"contractor:{project}",
            Action.DELIVERY_PUSH,
            Outcome.OK,
            parameters={"delivery_id": record.delivery_id},
        )
    )
    turn("contractor", tools=OTHER_TOOLS.fingerprint)  # after the push: not counted
    made.declare(project, record.delivery_id)
    while made.step():
        pass
    report = made.status(project, record.delivery_id).report
    assert report is not None
    lines = report.splitlines()
    flag = lines.index(
        "Turns before this delivery found other tools than the verification (each fingerprint, "
        "with how many turns found it):"
    )
    assert lines[flag + 2 : flag + 4] == [
        "- `none` (1 turn)",
        f"- `{OTHER_TOOLS.fingerprint}` (1 turn)",
    ]


def test_turns_that_all_found_the_tools_of_the_gate_raise_no_flag() -> None:
    made, project, audit, _store = service(tools=ScriptedTools(FIRST_TOOLS))
    ready(made, project)
    audit.append(
        AuditEvent(
            project,
            "hub",
            Action.TURN_START,
            Outcome.OK,
            parameters={"side": "contractor", "tools": FIRST_TOOLS.fingerprint},
        )
    )
    report = made.status(project, delivered(made, project)).report
    assert report is not None
    assert "found other tools" not in report
