"""The hub: one entry point that authenticates, authorizes, validates and logs every call.

Hub side of ATK-09, ATK-10, ATK-12, ATK-33, ATK-68, ATK-74, ATK-85 and of US-E1-07, US-E1-11,
US-E2-03.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.unit.core.contract_fixtures import note, put, requirement, wire
from tests.unit.core.hub_fixtures import Env, make_env, seed_contract
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.hub import TOOL_SPECS
from zephyros.core.rights import PERMISSIONS
from zephyros.core.tokens import Endpoint


@pytest.fixture
def env() -> Env:
    environment = make_env()
    seed_contract(environment)
    return environment


# ----------------------------------------------------------------------------- registry
def test_the_registry_and_the_matrix_describe_the_same_tools() -> None:
    from_registry = {spec.action for spec in TOOL_SPECS.values()}
    # The model gateway and the staging area are HTTP routes, not tools.
    routes = {Action.GATEWAY_CALL, Action.STAGING_READ, Action.DELIVERY_PUSH}
    assert from_registry | routes == set(PERMISSIONS)
    assert len(from_registry) == len(TOOL_SPECS)


def test_a_tool_is_listed_to_a_caller_only_if_the_caller_may_use_it(env: Env) -> None:
    def names(who: str) -> set[str]:
        return {spec.name for spec in env.hub.tools_for(env.callers[who])}

    reading = {"whoami", "project.list", "project.get", "contract.read", "delivery.status"}
    assert names("contractor_a") == reading | {"delivery.declare"}
    assert names("owner_a") == reading | {
        "contract.pending",
        "contract.propose",
        "contract.approve",
        "contract.reject",
        "acceptance.put",
        "inspection.record",
        "delivery.review",
    }
    assert names("main") == set(TOOL_SPECS) - {
        "contract.apply_reserved",
        "contract.import",
        "log.read",
        "log.verify",
        "acceptance.put",
        "delivery.declare",
    }
    assert names("operator") == set(TOOL_SPECS) - {"delivery.declare"}  # only the Contractor


def test_a_tool_is_found_by_its_name_or_its_mcp_name(env: Env) -> None:
    assert env.hub.find("contract.read") is env.hub.find("contract_read")
    assert env.hub.find("contract.read") is not None
    assert env.hub.find("contract-read") is None
    assert TOOL_SPECS["contract.apply_reserved"].mcp_name == "contract_apply_reserved"


# ----------------------------------------------------------------------------- identity
def test_whoami_answers_from_the_token_record(env: Env) -> None:
    response = env.call("contractor_a", "whoami")
    assert response.ok
    assert response.data == {
        "principal": f"contractor:{env.project_a}",
        "kind": "contractor",
        "side": "contractor",
        "project_id": env.project_a,
    }
    assert env.call("operator", "whoami").data["kind"] == "operator"  # type: ignore[index]  # reason: data set on success


@pytest.mark.parametrize(
    "field",
    ["principal", "side", "project", "role", "token", "user", "x_forwarded_for", "identity"],
)
def test_an_identity_claim_in_the_arguments_is_an_unknown_field(env: Env, field: str) -> None:
    response = env.call("contractor_a", "whoami", **{field: "owner"})
    assert not response.ok
    assert response.failure is not None
    assert response.failure.kind == "invalid"
    assert field in response.failure.message


def test_the_refusal_of_a_token_is_logged_once_with_the_principal_whose_token_it_is(
    env: Env,
) -> None:
    secret_caller = env.callers["owner_a"]
    issued = env.tokens.issue_project_token(secret_caller.principal, "i_x", "10.200.0.99", 600)
    before = env.record_count()
    # Presented from the wrong address: a copied token.
    assert (
        env.hub.authenticate(issued.secret, peer_address="10.200.0.18", endpoint=Endpoint.AGENT)
        is None
    )
    assert env.record_count() == before + 1
    record = env.last(env.project_a)
    assert (record.action, record.outcome, record.reason) == (
        Action.AUTHENTICATION,
        Outcome.REFUSED,
        Reason.WRONG_SOURCE,
    )
    assert record.principal == f"owner:{env.project_a}"
    assert json.loads(record.parameters)["source"] == "10.200.0.18"
    assert issued.secret not in record.parameters


def test_an_unknown_token_is_logged_as_unauthenticated_in_the_hub_stream(env: Env) -> None:
    before = len(env.audit.read("hub"))
    assert (
        env.hub.authenticate(
            "zph_c_" + "A" * 43, peer_address="192.168.1.5", endpoint=Endpoint.AGENT
        )
        is None
    )
    records = env.audit.read("hub")
    assert len(records) == before + 1
    assert records[-1].principal == "unauthenticated"
    assert records[-1].reason is Reason.UNKNOWN_TOKEN


# ----------------------------------------------------------------------------- ATK-09, ATK-10
CONTRACT_WRITES = [
    "contract.propose",
    "contract.approve",
    "contract.reject",
    "contract.apply_reserved",
]


@pytest.mark.parametrize("tool", CONTRACT_WRITES)
def test_the_contractor_cannot_write_the_contract_by_any_tool(env: Env, tool: str) -> None:
    """ATK-09: every tool that creates, updates or deletes, with a Contractor token."""
    before = env.contracts.read(env.principal("operator"), env.project_a)
    everything = {
        "summary": "tamper",
        "operations": [wire(put(requirement(9)))],
        "change_id": "c_00000001",
    }
    allowed_fields = {f.name for f in TOOL_SPECS[tool].inputs.fields}
    response = env.hub.call(
        env.callers["contractor_a"],
        tool,
        {k: v for k, v in everything.items() if k in allowed_fields},
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.kind == "forbidden"
    assert response.failure.reason is Reason.FORBIDDEN
    after = env.contracts.read(env.principal("operator"), env.project_a)
    assert after.id == before.id
    record = env.last(env.project_a)
    assert record.principal == f"contractor:{env.project_a}"
    assert record.action.value == tool
    assert record.outcome is Outcome.REFUSED


HOSTILE_TOOLS = [
    "config.set",
    "configuration.change",
    "gate.threshold",
    "method.update",
    "budget.raise",
    "network.protected_list",
    "approval.policy",
    "log.delete",
    "log.truncate",
    "log.edit",
    "contract.delete",
    "contract_write",
    "../contract",
    "contract.read\nseq=1",
    "",
    "*",
    "docker.run",
    "shell",
]


@pytest.mark.parametrize("tool", HOSTILE_TOOLS)
@pytest.mark.parametrize("who", ["contractor_a", "owner_a", "main", "operator"])
def test_a_tool_that_does_not_exist_is_refused_and_logged_whoever_asks(
    env: Env, who: str, tool: str
) -> None:
    """ATK-10 and ATK-68: nothing changes configuration or the log, because nothing offers to."""
    before = env.record_count()
    response = env.call(who, tool, path="/contract")
    assert not response.ok
    assert response.failure is not None
    assert (response.failure.kind, response.failure.reason) == (
        "unknown_tool",
        Reason.UNKNOWN_ACTION,
    )
    assert env.record_count() == before + 1
    stream = env.callers[who].principal.project_id or "hub"
    record = env.last(stream)
    assert record.action is Action.UNKNOWN
    assert json.loads(record.parameters)["requested"] == tool
    assert env.audit.verify(stream).ok


def test_the_log_tools_are_the_operators_alone(env: Env) -> None:
    for who in ("contractor_a", "owner_a", "main"):
        for tool in ("log.read", "log.verify"):
            response = env.call(who, tool, stream=env.project_a)
            assert not response.ok, (who, tool)
            assert response.failure is not None
            assert response.failure.kind == "forbidden"
    read = env.call("operator", "log.read", stream=env.project_a)
    assert read.ok
    assert read.data is not None
    assert all("\n" not in line for line in read.data["records"])  # type: ignore[attr-defined]  # reason: data is a mapping with a list
    verified = env.call("operator", "log.verify", stream=env.project_a)
    assert verified.data is not None
    assert verified.data["ok"] is True


# ----------------------------------------------------------------------------- ATK-33
SCOPED_CALLS = [
    ("project.get", {}),
    ("contract.read", {}),
    ("contract.read", {"version": 1}),
    ("contract.propose", {"summary": "x", "operations": [wire(put(note(7, "EXG-01")))]}),
    ("contract.approve", {"change_id": "c_00000001"}),
    ("contract.reject", {"change_id": "c_00000001"}),
]


@pytest.mark.parametrize(("tool", "extra"), SCOPED_CALLS)
@pytest.mark.parametrize("who", ["owner_b", "contractor_b"])
def test_an_instance_of_project_b_cannot_name_project_a(
    env: Env, who: str, tool: str, extra: dict[str, object]
) -> None:
    before = env.record_count()
    response = env.call(who, tool, project_id=env.project_a, **extra)
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason in (Reason.OTHER_PROJECT, Reason.FORBIDDEN)
    assert env.record_count() == before + 1
    # The refusal is in the log of the caller's own project, naming the caller.
    record = env.last(env.project_b)
    assert record.principal == env.callers[who].principal.id
    assert record.outcome is Outcome.REFUSED
    assert json.loads(record.parameters)["project_id"] == env.project_a


def test_the_same_calls_with_the_callers_own_project_succeed(env: Env) -> None:
    """The positive control of ATK-33."""
    seed_contract(env, env.project_b)
    for who in ("owner_b", "contractor_b"):
        assert env.call(who, "project.get", project_id=env.project_b).ok
        assert env.call(who, "project.get").ok
        read = env.call(who, "contract.read", project_id=env.project_b)
        assert read.ok
        assert read.data is not None
        assert read.data["project_id"] == env.project_b
        assert env.call(who, "contract.read", version=1).ok


def test_an_instance_sees_only_its_own_project_in_the_list(env: Env) -> None:
    listed = env.call("owner_b", "project.list")
    assert listed.data is not None
    own = [p["project_id"] for p in cast("list[dict[str, str]]", listed.data["projects"])]
    assert own == [env.project_b]
    everything = env.call("operator", "project.list")
    assert everything.data is not None
    assert len(cast("list[object]", everything.data["projects"])) == 2


# ------------------------------------------------------------------ contract flow and log
def test_the_approver_reads_the_pending_change_in_full_before_deciding(env: Env) -> None:
    """ADR 0020: the second real run's operator had to open the database to read a proposal."""
    proposed = env.call(
        "owner_a", "contract.propose", summary="note", operations=[wire(put(note(2, "EXG-02")))]
    )
    assert proposed.ok
    assert proposed.data is not None
    read = env.call("main", "contract.pending", project_id=env.project_a)
    assert read.ok
    assert read.data is not None
    [change] = cast("list[dict[str, object]]", read.data["changes"])
    assert change["change_id"] == proposed.data["change_id"]
    assert change["proposer"] == f"owner:{env.project_a}"
    assert change["summary"] == "note"
    assert change["operations"] == [wire(put(note(2, "EXG-02")))]
    assert env.audit.read(env.project_a)[-1].action is Action.CONTRACT_PENDING
    refused = env.call("contractor_a", "contract.pending", project_id=env.project_a)
    assert not refused.ok
    other = env.call("owner_b", "contract.pending", project_id=env.project_a)
    assert not other.ok


def test_every_action_on_the_contract_is_logged_with_author_action_and_time(env: Env) -> None:
    env.clock.advance(60)
    proposed = env.call(
        "owner_a", "contract.propose", summary="note", operations=[wire(put(note(2, "EXG-02")))]
    )
    assert proposed.ok
    assert proposed.data is not None
    change_id = proposed.data["change_id"]
    env.clock.advance(60)
    approved = env.call("main", "contract.approve", change_id=change_id, project_id=env.project_a)
    assert approved.ok
    assert approved.data is not None
    records = env.audit.read(env.project_a)
    propose_record, approve_record = records[-2], records[-1]
    assert (propose_record.principal, propose_record.action) == (
        f"owner:{env.project_a}",
        Action.CONTRACT_PROPOSE,
    )
    assert (approve_record.principal, approve_record.action) == (
        "main-agent",
        Action.CONTRACT_APPROVE,
    )
    assert approve_record.time > propose_record.time
    details = json.loads(approve_record.parameters)
    assert details["approver"] == "main-agent"
    assert details["version_id"] == approved.data["version_id"]
    assert env.audit.verify(env.project_a).ok


def test_a_refused_approval_is_logged_with_the_instance_the_action_and_the_time(env: Env) -> None:
    proposed = env.call(
        "owner_a", "contract.propose", summary="note", operations=[wire(put(note(2, "EXG-02")))]
    )
    assert proposed.data is not None
    response = env.call("owner_a", "contract.approve", change_id=proposed.data["change_id"])
    assert not response.ok
    record = env.last(env.project_a)
    assert (record.principal, record.action, record.reason) == (
        f"owner:{env.project_a}",
        Action.CONTRACT_APPROVE,
        Reason.SELF_APPROVAL,
    )
    assert record.time == "2026-10-10T12:00:00.000000Z"


def test_an_invalid_change_is_refused_with_its_faults_and_logged(env: Env) -> None:
    response = env.call(
        "owner_a",
        "contract.propose",
        summary="bad",
        operations=[{"op": "put", "object": {"type": "requirement"}}],
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason is Reason.INVALID_REQUEST
    assert env.last(env.project_a).reason is Reason.INVALID_REQUEST


def test_reserved_data_is_refused_through_the_tool_for_the_owner_side(env: Env) -> None:
    reserved = [wire(put(requirement(1, text="Weakened.", priority="P0")))]
    for who in ("owner_a", "main"):
        response = env.call(
            who, "contract.propose", project_id=env.project_a, summary="edit", operations=reserved
        )
        assert not response.ok
        assert response.failure is not None
        assert response.failure.reason is Reason.RESERVED_DATA


# ----------------------------------------------------------------------------- projects, ATK-74
HOSTILE_NAMES = [
    "--privileged",
    "../..",
    "$(touch /tmp/pwned)",
    "`id`",
    "a; rm -rf /",
    "name' OR '1'='1",
    "${IFS}x",
    "<script>alert(1)</script>",
    "x" * 100,
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_project_name_is_plain_text(env: Env, name: str) -> None:
    response = env.call(
        "operator",
        "project.create",
        name=name,
        repository_url="https://github.com/Taelyos/zephyros",
    )
    assert response.ok
    assert response.data is not None
    project_id = str(response.data["project_id"])
    assert project_id.startswith("p_")
    assert name not in project_id
    stored = env.projects.get(project_id)
    assert (stored.name, stored.repository_url) == (
        name,
        "https://github.com/Taelyos/zephyros",
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ssh://git@github.com/Taelyos/zephyros.git",
        "git@github.com:Taelyos/zephyros.git",
        "http://169.254.169.254/Taelyos/zephyros",
        "https://gitlab.com/Taelyos/zephyros",
        "https://github.com@evil.example.test/Taelyos/zephyros",
        "https://github.com/Taelyos/zephyros?token=1",
        "https://github.com/Taelyos",
    ],
)
def test_a_source_outside_the_annex_is_refused(env: Env, url: str) -> None:
    """EXG-44: only the https URLs of repositories hosted on GitHub, by exact host."""
    before = env.record_count()
    response = env.call("operator", "project.create", name="ok", repository_url=url)
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason.value == "invalid_request"
    assert "Accepted repository sources" in response.failure.message
    assert env.record_count() == before + 1


def test_a_source_of_the_annex_is_stored_as_written(env: Env) -> None:
    response = env.call(
        "operator",
        "project.create",
        name="fine",
        repository_url="https://github.com/Taelyos/zephyros.git",
    )
    assert response.ok
    assert response.data is not None
    stored = env.projects.get(str(response.data["project_id"]))
    assert stored.repository_url == "https://github.com/Taelyos/zephyros.git"


@pytest.mark.parametrize(
    "name", ["", "line\nbreak", "tab\there", "nul\x00", "esc\x1b[0m", "x" * 101]
)
def test_a_project_name_with_control_characters_or_a_bad_length_is_refused(
    env: Env, name: str
) -> None:
    before = len(env.projects.visible_to(env.principal("operator")))
    response = env.call("operator", "project.create", name=name)
    assert not response.ok
    assert len(env.projects.visible_to(env.principal("operator"))) == before


def test_a_project_request_with_a_field_that_is_not_defined_is_refused(env: Env) -> None:
    before = len(env.projects.visible_to(env.principal("operator")))
    for extra in (
        {"image": "evil"},
        {"command": "sh"},
        {"mount": "/"},
        {"env": {"A": "b"}},
        {"privileged": True},
    ):
        response = env.call("operator", "project.create", name="ok", **extra)
        assert not response.ok
        assert response.failure is not None
        assert response.failure.kind == "invalid"
    assert len(env.projects.visible_to(env.principal("operator"))) == before


def test_only_the_operator_and_the_main_agent_create_projects(env: Env) -> None:
    assert env.call("main", "project.create", name="from main").ok
    for who in ("owner_a", "contractor_a"):
        response = env.call(who, "project.create", name="no")
        assert not response.ok
        assert response.failure is not None
        assert response.failure.reason is Reason.FORBIDDEN


# ----------------------------------------------------------------------------- instances
def test_the_operator_starts_and_stops_an_instance_through_the_control(env: Env) -> None:
    started = env.call(
        "operator",
        "instance.start",
        project_id=env.project_a,
        side="contractor",
        prompt="Do the story.",
    )
    assert started.ok
    assert env.instances.prompts == [
        (env.project_a, env.principal("contractor_a").side, "Do the story.")
    ]
    status = env.call("main", "instance.status", project_id=env.project_a, side="contractor")
    assert status.data is not None
    assert status.data["state"] == "turn"
    record = next(r for r in env.audit.read(env.project_a) if r.action is Action.INSTANCE_START)
    assert "prompt_sha256" in json.loads(record.parameters)
    assert "Do the story." not in record.parameters
    assert env.call("operator", "instance.stop", project_id=env.project_a, side="contractor").ok
    gone = env.call("operator", "instance.status", project_id=env.project_a, side="contractor")
    assert gone.data is not None
    assert gone.data["state"] == "absent"


def test_an_instance_cannot_start_or_stop_instances(env: Env) -> None:
    for who in ("owner_a", "contractor_a"):
        for tool in ("instance.start", "instance.stop", "instance.status"):
            response = env.call(who, tool, side="owner", prompt="go")
            assert not response.ok, (who, tool)
    assert env.instances.prompts == []


def test_an_unknown_project_is_not_found_and_logged_in_the_hub_stream(env: Env) -> None:
    before = len(env.audit.read("hub"))
    response = env.call("operator", "contract.read", project_id="p_9999999999")
    assert not response.ok
    assert response.failure is not None
    assert response.failure.kind == "not_found"
    assert len(env.audit.read("hub")) == before + 1


def test_an_argument_that_is_not_an_object_is_refused(env: Env) -> None:
    for arguments in ([1], "text", 5, ["project_id"]):
        response = env.hub.call(env.callers["operator"], "whoami", arguments)
        assert not response.ok


# ----------------------------------------------------------------------------- one record per call
@settings(max_examples=120, deadline=None)
@given(
    who=st.sampled_from(["operator", "main", "owner_a", "contractor_a", "owner_b", "contractor_b"]),
    tool=st.one_of(st.sampled_from(sorted(TOOL_SPECS)), st.text(max_size=30)),
    arguments=st.one_of(
        st.none(),
        st.dictionaries(
            st.sampled_from(
                [
                    "project_id",
                    "side",
                    "prompt",
                    "version",
                    "change_id",
                    "summary",
                    "operations",
                    "stream",
                    "name",
                    "x",
                ]
            ),
            st.one_of(
                st.text(max_size=40),
                st.integers(),
                st.none(),
                st.booleans(),
                st.lists(st.integers(), max_size=2),
            ),
            max_size=5,
        ),
        st.text(max_size=10),
    ),
)
def test_every_call_leaves_exactly_one_record_and_a_sound_log(
    who: str, tool: str, arguments: object
) -> None:
    env = make_env()
    before = env.record_count()
    response = env.hub.call(env.callers[who], tool, arguments)
    assert env.record_count() == before + 1
    if response.ok:
        assert response.failure is None
    else:
        assert response.failure is not None
    for stream in env.audit.streams():
        assert env.audit.verify(stream).ok


def test_a_change_with_a_forbidden_term_is_flagged_and_logged(env: Env) -> None:
    """EXG-29: the flag rides along the change, in the answer and in the record of the log."""
    response = env.call(
        "operator",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="a vague requirement",
        operations=[wire(put(requirement(7, text="The screen should be simple and fast.")))],
    )
    assert response.ok, response.failure
    data = response.data if response.data is not None else {}
    assert data["flagged"] == {"EXG-07": ["fast", "simple", "should"]}
    payload = json.loads(env.last(env.project_a).parameters)
    assert payload["flagged"] == {"EXG-07": ["fast", "simple", "should"]}


def test_a_change_without_a_forbidden_term_says_so(env: Env) -> None:
    response = env.call(
        "operator",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="a precise requirement",
        operations=[wire(put(requirement(8, text="The system shall keep 90 % of the branches.")))],
    )
    assert response.ok, response.failure
    data = response.data if response.data is not None else {}
    assert data["flagged"] == {}


# ----------------------------------------------------------------------------- US-E7-01
def test_a_proposed_change_waits_for_the_owner_and_a_decision_ends_it(env: Env) -> None:
    """EXG-21/22: the hub keeps the task state; the side's calls and the event that was
    expected move the ladder."""
    response = env.call(
        "main",
        "contract.propose",
        project_id=env.project_a,
        summary="a change",
        operations=[wire(put(note(7, "EXG-01")))],
    )
    assert response.ok, response.failure
    assert env.continuity is not None
    waiting = list(env.continuity.waiting())
    assert [(state.side, state.kind) for state in waiting] == [("owner", "decision")]
    # a call of the owner side counts as activity: the step waits from it
    before = waiting[0].since
    env.clock.advance(800)
    assert env.call("owner_a", "contract.read", project_id=env.project_a).ok
    assert next(iter(env.continuity.waiting())).since == before  # the state keeps its origin
    (step,) = env.continuity.due(env.clock.now() + timedelta(seconds=900))
    assert step.kind == "wake"
    # the decision ends the expectation
    change_id = str(response.data["change_id"])  # type: ignore[index]  # reason: ok checked
    assert env.call("owner_a", "contract.approve", project_id=env.project_a, change_id=change_id).ok
    assert list(env.continuity.waiting()) == []


def test_the_events_of_the_delivery_loop_reach_the_main_agent(env: Env) -> None:
    env.hub.notified(
        "staging.prepared",
        {"project_id": env.project_a, "repository": "example/calc", "branch": "main"},
    )
    assert env.continuity is not None
    assert [(s.side, s.kind) for s in env.continuity.waiting()] == [("contractor", "delivery")]
    env.hub.notified(
        "delivery.decided",
        {"project_id": env.project_a, "delivery_id": "d_0001", "decision": "PASS"},
    )
    assert env.notifications == [
        (
            "delivery.decided",
            {
                "project_id": env.project_a,
                "delivery_id": "d_0001",
                "decision": "PASS",
                "simulation": False,
            },
        )
    ]
    assert env.last(env.project_a).action.value == "notification.sent"


def test_a_refused_notification_is_logged_with_its_reason(env: Env) -> None:
    """EXG-48: the hub refused the host before sending; the log says so, with the code."""
    env.refuse_notifications.append("elsewhere.example")
    env.hub.notified(
        "delivery.decided",
        {"project_id": env.project_a, "delivery_id": "d_0001", "decision": "PASS"},
    )
    records = list(env.records())
    refused = [r for r in records if r.action is Action.EGRESS]
    assert refused
    assert refused[-1].outcome is Outcome.REFUSED
    assert refused[-1].reason is Reason.EGRESS
    assert json.loads(refused[-1].parameters)["host"] == "elsewhere.example"
    sent = [r for r in records if r.action is Action.NOTIFICATION]
    assert sent
    assert sent[-1].outcome is Outcome.ERROR
    assert json.loads(sent[-1].parameters)["error"] == Reason.EGRESS.value
    assert env.notifications == []  # nothing was sent


# ----------------------------------------------------------------------------- ATK-71
def test_a_flood_of_refusals_is_rate_limited_and_still_recorded() -> None:
    """ATK-71: beyond the cap, the hub answers without the work — and every refusal, the
    throttle's included, stays in the log; other instances and the operator are untouched."""
    env = make_env(refusals_per_minute=3)
    for _ in range(3):
        response = env.call("contractor_a", "no.such.tool")
        assert response.failure is not None
        assert response.failure.reason is Reason.UNKNOWN_ACTION
    throttled = env.call("contractor_a", "no.such.tool")
    assert throttled.failure is not None
    assert throttled.failure.reason is Reason.RATE_LIMITED
    assert "Wait a minute" in throttled.failure.action
    # the flood is recorded, the throttle's own refusal too
    reasons = [r.reason for r in env.records() if r.outcome is Outcome.REFUSED]
    assert reasons.count(Reason.RATE_LIMITED) == 1
    assert len([r for r in reasons if r is Reason.UNKNOWN_ACTION]) >= 3
    # another instance, and the operator, keep their normal answer
    other = env.call("owner_a", "no.such.tool")
    assert other.failure is not None
    assert other.failure.reason is Reason.UNKNOWN_ACTION
    operator = env.call("operator", "no.such.tool")
    assert operator.failure is not None
    assert operator.failure.reason is Reason.UNKNOWN_ACTION


def test_a_simulated_run_marks_its_events(env: Env) -> None:
    """EXG-46: the events of a delivery carried with the simulated provider say so."""
    from tests.unit.core.hub_fixtures import make_env as build

    simulated = build(simulation=True)
    simulated.hub.notified(
        "delivery.decided",
        {"project_id": simulated.project_a, "delivery_id": "d_1", "decision": "PASS"},
    )
    assert simulated.notifications[-1][1]["simulation"] is True


def test_the_owner_learns_the_shape_of_a_change_from_the_tool_itself() -> None:
    """Hermes does not read MCP instructions: the second real run's Owner guessed the shape of
    an operation 22 times. The description and the field say it."""
    spec = TOOL_SPECS["contract.propose"]
    operations = next(field for field in spec.inputs.fields if field.name == "operations")
    assert '"op": "put", "object"' in operations.description
    assert '"op": "delete", "id"' in operations.description
    for shape in ("US-E1-01.1", "LNK:US-E1-01>EXG-01", "operator-reserved"):
        assert shape in spec.description
