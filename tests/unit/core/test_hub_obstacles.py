"""The obstacle tools of the hub (ADR 0034): each step is a call, logged once; the chain goes
up by decision; the continuity carries a blocking obstacle; the main agent hears of an obstacle
that reaches the operator or the human; an answer never shows the Contractor a hidden test."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from tests.unit.core.contract_fixtures import put, story, wire
from tests.unit.core.hub_fixtures import Env, attach_deliveries, make_env, seed_contract
from tests.unit.core.test_hub import (
    base_prepared,
    criterion_with_test,
    delivery_task_log,
    propose,
)
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.clock import format_time
from zephyros.core.continuity import MAIN_AGENT, OBSTACLE, RAISED, WAKE
from zephyros.core.deliveries import DeliveryRecord, DeliveryState
from zephyros.core.hub import TOOL_SPECS, ToolResponse
from zephyros.core.instances import InstanceInfo, InstanceState
from zephyros.core.obstacles import MAX_PER_TURN
from zephyros.core.ports.agent_engine import Side

EVIDENCE = [
    {"kind": "command", "command": "sim save run.dat", "output": "saving...", "exit_code": 0}
]
REPORT: dict[str, Any] = {
    "nature": "tool",
    "blocking": True,
    "title": "the simulator says it saves the run and never does",
    "description": "sim save prints 'saving...' and returns 0; no file is ever written",
    "evidence": EVIDENCE,
    "tried": "three times, with and without --sync",
    "needed": "a simulator that writes the run",
}
HIDDEN = (
    "import subprocess\n\n\ndef test_score():\n"
    "    out = subprocess.run(['sim', 'score', 'run.dat'], capture_output=True, text=True)\n"
    "    assert out.returncode == 0, out.stderr\n"
    "    assert float(out.stdout) >= 0.87, 'the run scores at least 0.87 on the hidden track'\n"
)


@pytest.fixture
def env() -> Env:
    return make_env()


def ok(response: ToolResponse) -> dict[str, Any]:
    assert response.ok, response.failure
    assert response.data is not None
    return dict(response.data)


def refused(response: ToolResponse, reason: Reason) -> str:
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason is reason, response.failure
    return response.failure.message


def report(env: Env, who: str = "contractor_a", **changes: Any) -> str:
    return str(ok(env.call(who, "obstacle.report", **{**REPORT, **changes}))["obstacle_id"])


def logged(env: Env, project: str, action: Action) -> list[dict[str, Any]]:
    return [
        {"outcome": r.outcome.value, "reason": r.reason.value, **json.loads(r.parameters)}
        for r in env.audit.read(project)
        if r.action is action
    ]


def tasks(env: Env) -> dict[str, tuple[str, str]]:
    assert env.continuity is not None
    return {state.kind: (state.side, state.detail) for state in env.continuity.waiting()}


def test_every_tool_has_a_closed_input_and_says_when_to_use_it() -> None:
    report_spec = TOOL_SPECS["obstacle.report"]
    names = [field.name for field in report_spec.inputs.fields]
    assert names == [
        "project_id",
        "nature",
        "blocking",
        "title",
        "description",
        "evidence",
        "tried",
        "needed",
    ]
    schema = report_spec.inputs.json_schema()
    assert schema["additionalProperties"] is False
    for words in ("after a real attempt", "Not to ask how to build", "piece of evidence"):
        assert words in report_spec.description
    assert "never copy an acceptance test" in TOOL_SPECS["obstacle.answer"].description


# ----------------------------------------------------------------------------- the chain
def test_a_blocking_obstacle_of_the_contractor_wakes_the_owner_side_at_once(env: Env) -> None:
    obstacle = report(env)
    assert obstacle == "OB-001"
    (record,) = logged(env, env.project_a, Action.OBSTACLE_REPORT)
    assert record["outcome"] == "ok"
    assert (record["obstacle_id"], record["nature"], record["blocking"], record["level"]) == (
        "OB-001",
        "tool",
        True,
        "owner",
    )
    assert record["title"] == REPORT["title"]
    assert record["evidence"] == ["command"]
    assert tasks(env) == {
        OBSTACLE: (
            "owner",
            "obstacle OB-001 (tool, blocking) of the Contractor awaits an answer of the Owner side",
        )
    }
    assert env.continuity is not None
    (step,) = env.continuity.due(env.clock.now())
    assert (step.kind, step.expectation.side) == (WAKE, "owner")
    assert "obstacle.read" in step.prompt
    assert "obstacle.answer" in step.prompt
    # the chain stays below the operator: the main agent hears nothing yet
    assert env.send_notifications() == []


def test_a_call_of_the_owner_side_is_no_answer(env: Env) -> None:
    report(env)
    assert env.continuity is not None
    env.clock.advance(5)
    ok(env.call("owner_a", "contract.read"))
    (step,) = env.continuity.due(env.clock.now())
    assert step.kind == WAKE


def test_a_hampering_obstacle_waits_for_its_reader(env: Env) -> None:
    report(env, blocking=False)
    assert tasks(env) == {}


def test_the_owner_side_escalates_and_the_main_agent_hears_of_it(env: Env) -> None:
    report(env)
    answered = ok(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="escalate",
            text="only the operator can change the simulator",
        )
    )
    assert (answered["status"], answered["awaits"]) == ("escalated", "operator")
    assert tasks(env) == {
        RAISED: (
            MAIN_AGENT,
            "obstacle OB-001 (tool, blocking) of the Contractor awaits an answer of the operator "
            "or the main agent",
        )
    }
    assert env.continuity is not None
    assert env.continuity.due(env.clock.now()) == []  # the event told the main agent already
    ((event, payload),) = env.send_notifications()
    assert event == "obstacle.raised"
    assert payload == {
        "project_id": env.project_a,
        "obstacle_id": "OB-001",
        "side": "contractor",
        "nature": "tool",
        "blocking": True,
        "title": REPORT["title"],
        "level": "operator",
        "by": f"owner:{env.project_a}",
        "simulation": False,
    }
    # the operator sees it awaiting, without reading the log
    waiting = ok(env.call("operator", "obstacle.list", status="awaiting", level="operator"))[
        "obstacles"
    ]
    assert [(o["project_id"], o["obstacle_id"]) for o in waiting] == [(env.project_a, "OB-001")]


def test_the_main_agent_escalates_to_the_human_whom_the_operator_channel_answers(
    env: Env,
) -> None:
    report(env)
    ok(env.call("owner_a", "obstacle.answer", obstacle_id="OB-001", decision="escalate", text="x"))
    ok(
        env.call(
            "main",
            "obstacle.answer",
            project_id=env.project_a,
            obstacle_id="OB-001",
            decision="escalate",
            text="the machine of the simulator needs a person",
        )
    )
    assert tasks(env) == {}  # the human has no ladder: the event is the call
    events = env.send_notifications()
    assert [(e, p["level"], p["by"]) for e, p in events] == [
        ("obstacle.raised", "operator", f"owner:{env.project_a}"),
        ("obstacle.raised", "human", "main-agent"),
    ]
    sent = logged(env, env.project_a, Action.NOTIFICATION)
    assert [(s["event"], s["level"]) for s in sent] == [
        ("obstacle.raised", "operator"),
        ("obstacle.raised", "human"),
    ]
    human = ok(env.call("operator", "obstacle.list", status="awaiting", level="human"))
    assert human["count"] == 1
    # the human decides, through the operator channel; the main agent may not
    refused(
        env.call(
            "main",
            "obstacle.answer",
            project_id=env.project_a,
            obstacle_id="OB-001",
            decision="handle",
            action="relaunched",
            text="done",
        ),
        Reason.FORBIDDEN,
    )
    handled = ok(
        env.call(
            "operator",
            "obstacle.answer",
            project_id=env.project_a,
            obstacle_id="OB-001",
            decision="handle",
            action="environment_fixed",
            text="the simulator writes its runs again; start a new turn",
        )
    )
    assert (handled["status"], handled["awaits"]) == ("answered", None)


def test_a_secret_in_a_title_is_masked_in_the_event(env: Env) -> None:
    token = "sk-" + "a" * 30
    report(env, "owner_a", title=f"the key {token} is refused")
    ((_event, payload),) = env.send_notifications()
    assert token not in str(payload)
    assert payload["title"] == "the key [masked] is refused"


def test_no_secret_pasted_into_an_obstacle_is_kept_or_shown(env: Env) -> None:
    """EXG-49: the output of `env`, given as evidence, holds the token of the instance and a
    provider key; a known secret of the hub is in the description. They are masked as they are
    written: the store, the readers and the log never hold them."""
    hub_token = "zph_c_" + "T" * 43
    provider_key = "sk-" + "k" * 32
    known = "canary-credential-123"
    env.hub._secrets = lambda: [known]  # reason: a hub that holds a provider credential
    leaked = f"ZEPHYROS_TOKEN={hub_token}\nOPENAI_API_KEY={provider_key}\nHOME=/opt/data"
    evidence = [{"kind": "command", "command": "env", "output": leaked, "note": provider_key}]
    report(env, evidence=evidence, description=f"the gateway refuses {known}", tried=hub_token)
    ok(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="reject",
            text=f"never paste {hub_token}: it is your own token",
        )
    )
    ok(
        env.call(
            "operator", "obstacle.close", project_id=env.project_a, obstacle_id="OB-001", note=known
        )
    )
    assert env.obstacles is not None
    for text in (
        json.dumps(ok(env.call("owner_a", "obstacle.read", obstacle_id="OB-001"))),
        json.dumps(ok(env.call("contractor_a", "obstacle.read", obstacle_id="OB-001"))),
        repr(env.obstacles.get(env.principal("operator"), env.project_a, "OB-001")),
        " ".join(r.parameters for r in env.audit.read(env.project_a)),
    ):
        for secret in (hub_token, provider_key, known):
            assert secret not in text
    read = ok(env.call("owner_a", "obstacle.read", obstacle_id="OB-001"))
    assert read["evidence"][0]["output"] == (
        "ZEPHYROS_TOKEN=[masked]\nOPENAI_API_KEY=[masked]\nHOME=/opt/data"
    )
    assert read["evidence"][0]["note"] == "[masked]"
    assert read["description"] == "the gateway refuses [masked]"
    assert read["tried"] == "[masked]"
    assert read["decisions"][0]["text"] == "never paste [masked]: it is your own token"
    assert read["decisions"][1]["text"] == "[masked]"


def test_an_obstacle_of_the_owner_side_goes_to_the_operator(env: Env) -> None:
    obstacle = report(env, "owner_a", nature="budget", title="no turn fits the bench")
    assert tasks(env)[RAISED][0] == MAIN_AGENT
    ((event, payload),) = env.send_notifications()
    assert (event, payload["level"], payload["side"], payload["by"]) == (
        "obstacle.raised",
        "operator",
        "owner",
        f"owner:{env.project_a}",
    )
    refused(
        env.call("owner_a", "obstacle.answer", obstacle_id=obstacle, decision="clarify", text="x"),
        Reason.FORBIDDEN,
    )


def test_the_author_is_told_of_the_answer_at_its_next_turn(env: Env) -> None:
    report(env)
    ok(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="reject",
            text="a fault of your delivery: d2 writes its run in /tmp",
        )
    )
    assert env.obstacles is not None
    briefing = env.obstacles.brief(env.project_a, Side.CONTRACTOR)
    assert briefing is not None
    assert briefing.answered == ("OB-001",)
    read = ok(env.call("contractor_a", "obstacle.read", obstacle_id="OB-001"))
    assert read["status"] == "rejected"
    assert read["decisions"][0]["text"] == "a fault of your delivery: d2 writes its run in /tmp"
    assert env.obstacles.brief(env.project_a, Side.CONTRACTOR) is None
    closed = ok(env.call("contractor_a", "obstacle.close", obstacle_id="OB-001", note="right"))
    assert closed["status"] == "closed"


# ----------------------------------------------------------------------------- who reads
def test_the_contractor_never_reads_an_obstacle_of_the_owner_side(env: Env) -> None:
    report(env, "owner_a")
    message = refused(
        env.call("contractor_a", "obstacle.read", obstacle_id="OB-001"), Reason.FORBIDDEN
    )
    assert "OB-001" in message
    assert ok(env.call("contractor_a", "obstacle.list"))["obstacles"] == []
    (record,) = logged(env, env.project_a, Action.OBSTACLE_READ)
    assert (record["outcome"], record["reason"]) == ("refused", "forbidden")


def test_an_instance_of_another_project_reads_and_answers_nothing(env: Env) -> None:
    report(env)
    for who, tool, extra in (
        ("owner_b", "obstacle.read", {}),
        ("contractor_b", "obstacle.list", {}),
        ("owner_b", "obstacle.answer", {"decision": "reject", "text": "x"}),
        ("contractor_b", "obstacle.close", {}),
    ):
        response = env.call(who, tool, project_id=env.project_a, obstacle_id="OB-001", **extra)
        if tool == "obstacle.list":
            response = env.call(who, tool, project_id=env.project_a)
        refused(response, Reason.OTHER_PROJECT)
    # its own project holds no such obstacle
    refused(env.call("owner_b", "obstacle.read", obstacle_id="OB-001"), Reason.NOT_FOUND)


def test_the_contractor_answers_nothing_and_the_operator_reports_nothing(env: Env) -> None:
    report(env)
    refused(
        env.call(
            "contractor_a", "obstacle.answer", obstacle_id="OB-001", decision="clarify", text="x"
        ),
        Reason.FORBIDDEN,
    )
    refused(
        env.call("operator", "obstacle.report", project_id=env.project_a, **REPORT),
        Reason.FORBIDDEN,
    )


def test_the_list_follows_the_reader_and_its_filters(env: Env) -> None:
    for who, changes in (
        ("contractor_a", {}),
        ("contractor_a", {"blocking": False, "title": "pip is slow"}),
        ("owner_a", {"title": "a question for the operator"}),
        ("contractor_b", {}),
    ):
        report(env, who, **changes)
        env.clock.advance(1)  # the list is the most recent first
    ok(env.call("owner_a", "obstacle.answer", obstacle_id="OB-002", decision="clarify", text="ok"))

    def ids(who: str, **filters: Any) -> list[str]:
        found = ok(env.call(who, "obstacle.list", **filters))["obstacles"]
        return [f"{o['project_id'][-2:]}/{o['obstacle_id']}" for o in found]

    a, b = env.project_a[-2:], env.project_b[-2:]
    assert ids("contractor_a") == [f"{a}/OB-002", f"{a}/OB-001"]
    assert ids("owner_a") == [f"{a}/OB-003", f"{a}/OB-002", f"{a}/OB-001"]
    assert ids("operator") == [f"{b}/OB-001", f"{a}/OB-003", f"{a}/OB-002", f"{a}/OB-001"]
    assert ids("main", project_id=env.project_b) == [f"{b}/OB-001"]
    assert ids("owner_a", status="awaiting", level="owner") == [f"{a}/OB-001"]
    assert ids("owner_a", status="answered") == [f"{a}/OB-002"]
    assert ids("owner_a", side="owner") == [f"{a}/OB-003"]
    assert ids("operator", limit=1) == [f"{b}/OB-001"]
    assert ok(env.call("operator", "obstacle.list", limit=1))["count"] == 4
    record = logged(env, "hub", Action.OBSTACLE_LIST)[-1]
    assert (record["count"], record["project_id"]) == (4, None)


# ----------------------------------------------------------------------------- the decisions
def test_each_decision_names_what_goes_with_it(env: Env) -> None:
    report(env)
    for decision, extra, said in (
        ("amend", {}, "amend names its change_id"),
        ("fix_test", {}, "fix_test names its paths"),
        ("clarify", {"change_id": "c_00000001"}, "change_id goes with the decision amend only"),
        ("reject", {"paths": ["t.py"]}, "paths goes with the decision fix_test only"),
    ):
        message = refused(
            env.call(
                "owner_a",
                "obstacle.answer",
                obstacle_id="OB-001",
                decision=decision,
                text="x",
                **extra,
            ),
            Reason.INVALID_REQUEST,
        )
        assert said in message


def test_an_amendment_names_a_change_of_the_contract_that_stands(env: Env) -> None:
    seed_contract(env)
    report(env, nature="contract")
    refused(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="amend",
            change_id="c_99999999",
            text="see the change",
        ),
        Reason.NOT_FOUND,
    )
    proposed = ok(
        env.call(
            "owner_a",
            "contract.propose",
            summary="the story the Contractor asked about",
            operations=[wire(put(story(3, 1)))],
        )
    )
    change = str(proposed["change_id"])
    ok(env.call("operator", "contract.reject", project_id=env.project_a, change_id=change))
    message = refused(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="amend",
            change_id=change,
            text="see the change",
        ),
        Reason.INVALID_REQUEST,
    )
    assert "was rejected" in message
    again = str(
        ok(
            env.call(
                "owner_a",
                "contract.propose",
                summary="the story, again",
                operations=[wire(put(story(3, 1)))],
            )
        )["change_id"]
    )
    answered = ok(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="amend",
            change_id=again,
            text="the story is now in the contract, once approved",
        )
    )
    assert answered["status"] == "answered"
    (record,) = [
        r for r in logged(env, env.project_a, Action.OBSTACLE_ANSWER) if r["outcome"] == "ok"
    ]
    assert (record["decision"], record["change_id"], record["from_level"]) == (
        "amend",
        again,
        "owner",
    )


def test_a_fixed_test_is_one_the_hub_keeps(env: Env) -> None:
    attach_deliveries(env)
    report(env, nature="acceptance_test")
    refused(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="fix_test",
            paths=["tests/test_score.py"],
            text="fixed",
        ),
        Reason.NOT_FOUND,
    )
    ok(env.call("owner_a", "acceptance.put", path="tests/test_score.py", content=HIDDEN))
    answered = ok(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="fix_test",
            paths=["tests/test_score.py"],
            text="the test no longer needs the network",
        )
    )
    assert answered["status"] == "answered"


# ----------------------------------------------------------------------------- the leak
def test_an_answer_that_copies_a_hidden_test_is_refused(env: Env) -> None:
    attach_deliveries(env)
    ok(env.call("owner_a", "acceptance.put", path="tests/test_score.py", content=HIDDEN))
    report(env, nature="acceptance_test")
    message = refused(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="clarify",
            text="Here is the test, so you see:\n" + HIDDEN,
        ),
        Reason.ACCEPTANCE_LEAK,
    )
    assert "tests/test_score.py" in message
    (record,) = logged(env, env.project_a, Action.OBSTACLE_ANSWER)
    assert (record["outcome"], record["reason"]) == ("refused", "acceptance_leak")
    # an escalation is read by the Contractor too
    refused(
        env.call(
            "owner_a", "obstacle.answer", obstacle_id="OB-001", decision="escalate", text=HIDDEN
        ),
        Reason.ACCEPTANCE_LEAK,
    )
    # nothing was written: the obstacle still awaits, and an answer in plain words goes
    answered = ok(
        env.call(
            "owner_a",
            "obstacle.answer",
            obstacle_id="OB-001",
            decision="clarify",
            text="The test scores your run with the simulator and wants 0.87 or more.",
        )
    )
    assert answered["status"] == "answered"
    # the operator's note when it closes is held to the same rule
    refused(
        env.call(
            "operator",
            "obstacle.close",
            project_id=env.project_a,
            obstacle_id="OB-001",
            note=HIDDEN,
        ),
        Reason.ACCEPTANCE_LEAK,
    )


def test_the_owner_side_may_quote_its_tests_to_the_operator(env: Env) -> None:
    """An obstacle of the Owner side is never read by the Contractor."""
    attach_deliveries(env)
    ok(env.call("owner_a", "acceptance.put", path="tests/test_score.py", content=HIDDEN))
    report(env, "owner_a", nature="acceptance_test", description="it reads:\n" + HIDDEN)
    answered = ok(
        env.call(
            "operator",
            "obstacle.answer",
            project_id=env.project_a,
            obstacle_id="OB-001",
            decision="clarify",
            text="about this line: " + HIDDEN,
        )
    )
    assert answered["status"] == "answered"


# ----------------------------------------------------------------------------- the evidence
def test_the_evidence_names_what_exists_in_the_project(env: Env) -> None:
    service = attach_deliveries(env)
    refused(
        env.call(
            "contractor_a",
            "obstacle.report",
            **{**REPORT, "evidence": [{"kind": "delivery", "delivery_id": "d7"}]},
        ),
        Reason.NOT_FOUND,
    )
    service._store.put_record(  # reason: a delivery received, without the staging area
        DeliveryRecord(
            env.project_a, "d7", "add", DeliveryState.RECEIVED, format_time(env.clock.now())
        )
    )
    last = env.audit.read(env.project_a)[-1].seq
    refused(
        env.call(
            "contractor_a",
            "obstacle.report",
            **{**REPORT, "evidence": [{"kind": "log", "seq": last + 50}]},
        ),
        Reason.NOT_FOUND,
    )
    evidence = [
        {"kind": "delivery", "delivery_id": "d7"},
        {"kind": "log", "seq": last},
        {"kind": "path", "path": "/work/out", "note": "empty"},
    ]
    assert report(env, evidence=evidence) == "OB-001"
    read = ok(env.call("owner_a", "obstacle.read", obstacle_id="OB-001"))
    assert read["evidence"] == [
        {"kind": "delivery", "delivery_id": "d7"},
        {"kind": "log", "seq": last},
        {"kind": "path", "note": "empty", "path": "/work/out"},
    ]


def test_a_report_out_of_its_bounds_is_refused_by_its_schema(env: Env) -> None:
    changes: dict[str, Any]
    for changes in (
        {"title": "x" * 121},
        {"title": "two\nlines"},
        {"evidence": []},
        {"evidence": [{"kind": "path", "path": "/work/x"}] * 11},
        {"nature": "weather"},
        {"blocking": "yes"},
        {"evidence": [{"kind": "command", "command": "x", "output": "y" * 2001}]},
    ):
        refused(
            env.call("contractor_a", "obstacle.report", **{**REPORT, **changes}),
            Reason.INVALID_REQUEST,
        )
    missing = dict(REPORT)
    del missing["tried"]
    refused(env.call("contractor_a", "obstacle.report", **missing), Reason.INVALID_REQUEST)


# ----------------------------------------------------------------------------- the bounds
def test_the_bounds_of_a_turn_follow_the_turn_the_hub_runs(env: Env) -> None:
    started = format_time(env.clock.now())

    def status(project_id: str, side: Side) -> InstanceInfo:
        return InstanceInfo(project_id, side, InstanceState.TURN, turn_started=started)

    env.instances.status = status  # type: ignore[method-assign]  # reason: a turn under way
    for _ in range(MAX_PER_TURN):
        report(env, blocking=False)
    refused(env.call("contractor_a", "obstacle.report", **REPORT), Reason.TOO_MANY_OBSTACLES)
    env.clock.advance(60)
    started = format_time(env.clock.now())
    assert report(env) == "OB-004"


def contractor_ladder(env: Env) -> list[tuple[int, list[str]]]:
    """The Contractor's ladder from now: each step it takes, with the seconds it waited."""
    assert env.continuity is not None
    walked: list[tuple[int, list[str]]] = []
    for _ in range(5):
        for waited in range(0, 7200, 60):
            due = [
                step
                for step in env.continuity.due(env.clock.now() + timedelta(seconds=waited))
                if step.expectation.side == "contractor"
            ]
            if due:
                walked.append((waited, [step.kind for step in due]))
                env.clock.advance(waited)
                for step in due:
                    env.continuity.taken(step, env.clock.now())
                break
    return walked


@pytest.mark.parametrize("blocking", [True, False])
def test_an_obstacle_moves_no_deadline(blocking: bool) -> None:
    """ADR 0034: a report is a call like any other. The Contractor's task is neither held nor
    given more time: its ladder after a report is the ladder after any other call of its."""
    walked = []
    for reported in (True, False):
        env = make_env()
        seed_contract(env)
        change = propose(env, "owner_a", env.project_a, criterion_with_test())
        ok(env.call("operator", "contract.approve", project_id=env.project_a, change_id=change))
        base_prepared(env, env.project_a)  # the Contractor's ladder starts: a delivery awaited
        env.clock.advance(600)
        if reported:
            report(env, blocking=blocking)
        else:
            ok(env.call("contractor_a", "contract.read"))
        assert "hold" not in [r["step"] for r in delivery_task_log(env, env.project_a)]
        walked.append(contractor_ladder(env))
    assert walked[0] == walked[1]
    assert walked[0] == [
        (900, ["wake"]),
        (900, ["restart"]),
        (900, ["restart"]),
        (900, ["alert_main"]),
        (3600, ["alert_human"]),
    ]


# ----------------------------------------------------------------------------- the tasks
def test_the_task_follows_the_oldest_blocking_obstacle_and_ends_with_the_last(env: Env) -> None:
    report(env)
    env.clock.advance(10)
    report(env)
    ok(env.call("owner_a", "obstacle.answer", obstacle_id="OB-001", decision="clarify", text="x"))
    assert "OB-002" in tasks(env)[OBSTACLE][1]
    ok(env.call("contractor_a", "obstacle.close", obstacle_id="OB-002"))
    assert tasks(env) == {}


def test_the_tasks_follow_the_obstacles_at_the_start_of_the_hub(env: Env) -> None:
    report(env)
    assert env.continuity is not None
    env.continuity.answered(env.project_a, OBSTACLE)  # as if the hub stopped before it acted
    env.hub.check_tasks()
    assert OBSTACLE in tasks(env)


def test_a_hub_without_obstacles_says_so(env: Env) -> None:
    env.hub._obstacles = None  # reason: a hub built without the service
    refused(env.call("contractor_a", "obstacle.report", **REPORT), Reason.INVALID_REQUEST)


def test_the_wake_of_the_owner_side_counts_from_the_report(env: Env) -> None:
    report(env)
    assert env.continuity is not None
    env.clock.advance(int(timedelta(minutes=1).total_seconds()))
    (step,) = env.continuity.due(env.clock.now())
    env.continuity.taken(step, env.clock.now())
    assert env.continuity.due(env.clock.now() + timedelta(seconds=899)) == []
    (restart,) = env.continuity.due(env.clock.now() + timedelta(seconds=900))
    assert restart.kind == "restart"
    assert restart.expectation.kind == OBSTACLE
    assert any(r.action is Action.OBSTACLE_REPORT for r in env.audit.read(env.project_a))
    assert all(
        r.outcome is Outcome.OK
        for r in env.audit.read(env.project_a)
        if r.action is Action.OBSTACLE_REPORT
    )
