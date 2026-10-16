"""The hub: one entry point that authenticates, authorizes, validates and logs every call.

Hub side of ATK-09, ATK-10, ATK-12, ATK-33, ATK-68, ATK-74, ATK-85 and of US-E1-07, US-E1-11,
US-E2-03.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.unit.core.contract_fixtures import note, put, requirement, story, wire
from tests.unit.core.hub_fixtures import Env, attach_deliveries, make_env, seed_contract
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.contract import PendingChange
from zephyros.core.hub import NOTIFIED_EVENTS, TOOL_SPECS
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import OPERATOR, Principal
from zephyros.core.rights import PERMISSIONS
from zephyros.core.tokens import Endpoint, Rejected


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
    obstacles = {"obstacle.report", "obstacle.list", "obstacle.read", "obstacle.close"}
    assert names("contractor_a") == reading | obstacles | {"delivery.declare"}
    assert names("owner_a") == reading | obstacles | {
        "obstacle.answer",
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
        "obstacle.report",
        "obstacle.close",  # its author's or the operator's
    }
    # only an instance reports an obstacle; only the Contractor declares a delivery
    assert names("operator") == set(TOOL_SPECS) - {"delivery.declare", "obstacle.report"}


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
            "zph_c_" + "A" * 43, peer_address="192.168.7.5", endpoint=Endpoint.AGENT
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
def test_a_source_the_hub_does_not_accept_is_refused(env: Env, url: str) -> None:
    """EXG-44: only the https URLs of repositories hosted on GitHub, by exact host."""
    before = env.record_count()
    response = env.call("operator", "project.create", name="ok", repository_url=url)
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason.value == "invalid_request"
    assert "repository hosted on GitHub" in response.failure.message
    assert env.record_count() == before + 1


def test_an_accepted_source_is_stored_as_written(env: Env) -> None:
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


def test_a_refused_token_of_an_instance_reaches_its_status(env: Env) -> None:
    """AN-021: a turn that lost its token used to look `turn` and nothing more."""
    caller = env.callers["contractor_a"]
    assert env.tokens.revoke(caller.token_id)
    found = env.hub.identify(
        env.secrets["contractor_a"],
        peer_address=env.addresses["contractor_a"],
        endpoint=Endpoint.AGENT,
    )
    assert isinstance(found, Rejected)
    assert env.instances.refused == [(env.project_a, Side.CONTRACTOR, caller.token_id, "revoked")]
    status = env.call("operator", "instance.status", project_id=env.project_a, side="contractor")
    assert status.data is not None
    refused = status.data["token_refused"]
    assert isinstance(refused, dict)
    assert (refused["reason"], refused["token_id"]) == ("revoked", caller.token_id)
    clean = env.call("operator", "instance.status", project_id=env.project_a, side="owner")
    assert clean.data is not None
    assert clean.data["token_refused"] is None


def test_a_refusal_that_names_no_instance_reaches_no_instance(env: Env) -> None:
    assert env.tokens.revoke(env.callers["main"].token_id)
    for secret, endpoint in (
        (env.secrets["main"], Endpoint.MAIN_AGENT),
        ("zph_c_" + "x" * 43, Endpoint.AGENT),
        ("not a token", Endpoint.AGENT),
    ):
        found = env.hub.identify(secret, peer_address="10.200.0.18", endpoint=endpoint)
        assert isinstance(found, Rejected)
    assert env.instances.refused == []


def test_the_operator_starts_a_turn_in_a_new_session(env: Env) -> None:
    """ADR 0016, amended: a session gone wrong is left behind, on the operator's word."""
    started = env.call(
        "operator",
        "instance.start",
        project_id=env.project_a,
        side="owner",
        prompt="Start again.",
        session="new",
    )
    assert started.ok
    assert env.instances.new_sessions == [(env.project_a, env.principal("owner_a").side)]
    record = next(r for r in env.audit.read(env.project_a) if r.action is Action.INSTANCE_START)
    assert json.loads(record.parameters)["session"] == "new"


def test_a_turn_continues_its_session_unless_the_operator_says_new(env: Env) -> None:
    for session in ({}, {"session": "continue"}):
        assert env.call(
            "main", "instance.start", project_id=env.project_a, side="owner", prompt="go", **session
        ).ok
        env.call("operator", "instance.stop", project_id=env.project_a, side="owner")
    assert env.instances.new_sessions == []
    records = [r for r in env.audit.read(env.project_a) if r.action is Action.INSTANCE_START]
    assert all("session" not in json.loads(r.parameters) for r in records)


def test_only_the_operator_starts_a_new_session(env: Env) -> None:
    response = env.call(
        "main", "instance.start", project_id=env.project_a, side="owner", prompt="go", session="new"
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason is Reason.FORBIDDEN
    assert env.instances.prompts == []


def test_a_session_is_continue_or_new(env: Env) -> None:
    response = env.call(
        "operator",
        "instance.start",
        project_id=env.project_a,
        side="owner",
        prompt="go",
        session="fresh",
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason is Reason.INVALID_REQUEST


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


# ----------------------------------------------------------------------------- ADR 0023
def decision_steps(env: Env, after: int) -> list[tuple[str, str]]:
    """The step of the project's decision ladder due `after` seconds from now: who, and what."""
    assert env.continuity is not None
    steps = env.continuity.due(env.clock.now() + timedelta(seconds=after))
    return [(s.expectation.side, s.kind) for s in steps if s.expectation.kind == "decision"]


def test_a_later_proposal_does_not_start_the_decision_ladder_again(env: Env) -> None:
    """The ladder counts from the first change that awaits a decision; a proposal is not one."""
    propose(env, "main", env.project_a, note(7, "EXG-01"))
    env.clock.advance(600)
    propose(env, "main", env.project_a, note(8, "EXG-01"))
    assert decision_steps(env, after=299) == []
    assert decision_steps(env, after=300) == [("owner", "wake")]


def test_a_change_of_the_owner_side_awaits_the_main_agent_whatever_the_owner_side_does(
    env: Env,
) -> None:
    """The Owner side cannot approve its own change: waking it is no use, and its calls are no
    sign that the decision is coming. The main agent is alerted, then the human."""
    first = propose(env, "owner_a", env.project_a, note(7, "EXG-01"))
    env.clock.advance(800)
    assert env.call("owner_a", "contract.read", project_id=env.project_a).ok
    propose(env, "owner_a", env.project_a, note(8, "EXG-01"))
    assert decision_steps(env, after=99) == []
    assert decision_steps(env, after=100) == [("main-agent", "alert_main")]
    assert env.continuity is not None
    (alert,) = env.continuity.due(env.clock.now() + timedelta(seconds=100))
    assert first in alert.expectation.detail
    assert "the operator or the main agent decides" in alert.expectation.detail
    env.continuity.taken(alert, env.clock.now() + timedelta(seconds=100))
    assert decision_steps(env, after=100 + 3599) == []
    assert decision_steps(env, after=100 + 3600) == [("main-agent", "alert_human")]


def test_a_decision_that_leaves_another_change_moves_the_task_to_it(env: Env) -> None:
    """ADR 0021 left the decision task ended by any decision, even with another change still to
    decide. The task now moves to the oldest change left, carried by whoever decides that one,
    and counts from the decision."""
    owners = propose(env, "owner_a", env.project_a, note(7, "EXG-01"))
    env.clock.advance(60)
    propose(env, "main", env.project_a, note(8, "EXG-01"))
    assert decision_steps(env, after=840) == [("main-agent", "alert_main")]
    env.clock.advance(600)
    assert env.call("main", "contract.reject", project_id=env.project_a, change_id=owners).ok
    assert decision_steps(env, after=899) == []
    assert decision_steps(env, after=900) == [("owner", "wake")]


def test_a_decision_moves_the_task_to_the_next_change_of_the_same_carrier(env: Env) -> None:
    """The next change may have the same carrier: the task is still a new one, counted from the
    decision, not from the change decided. (The operator decides here: a call of the Owner side
    would count as its activity anyway.)"""
    first = propose(env, "main", env.project_a, note(7, "EXG-01"))
    env.clock.advance(60)
    propose(env, "main", env.project_a, note(8, "EXG-01"))
    env.clock.advance(600)
    assert env.call("operator", "contract.reject", project_id=env.project_a, change_id=first).ok
    assert decision_steps(env, after=899) == []
    assert decision_steps(env, after=900) == [("owner", "wake")]


def test_a_version_that_leaves_no_change_to_approve_ends_the_decision(env: Env) -> None:
    """A change proposed on an older version can no longer be approved: no one is woken for
    it, whoever wrote the new version."""
    propose(env, "main", env.project_a, note(7, "EXG-01"))
    assert env.call(
        "operator",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="one more requirement",
        operations=[wire(put(requirement(4)))],
    ).ok
    assert decision_steps(env, after=86_400) == []


def criterion_with_test(
    story_id: str = "US-E1-02", test: str = "checks/sub.txt::test_sub"
) -> dict[str, str]:
    """A criterion that names its acceptance test. US-E1-02 covers a P1 requirement of the seed
    contract, so the Owner side may propose it; a criterion of US-E1-01 (P0) is the operator's."""
    return {
        "type": "criterion",
        "id": f"{story_id}.2",
        "story": story_id,
        "text": "The system shall give the result.",
        "test": test,
    }


def base_prepared(env: Env, project: str) -> None:
    env.hub.notified(
        "staging.prepared", {"project_id": project, "repository": "example/calc", "branch": "main"}
    )


def propose(env: Env, who: str, project: str, item: dict[str, str]) -> str:
    response = env.call(
        who,
        "contract.propose",
        project_id=project,
        summary="a change",
        operations=[wire(put(item))],
    )
    assert response.ok, response.failure
    return str(response.data["change_id"])  # type: ignore[index]  # reason: ok checked


def contractor_steps(env: Env, after: int, project: str | None = None) -> list[str]:
    """The step of the Contractor's ladder due `after` seconds from now, if any."""
    assert env.continuity is not None
    steps = env.continuity.due(env.clock.now() + timedelta(seconds=after))
    return [
        step.kind
        for step in steps
        if step.expectation.side == "contractor"
        and step.expectation.project_id == (project or env.project_a)
    ]


def test_a_base_prepared_before_the_contract_is_ready_wakes_no_contractor(env: Env) -> None:
    """The comparison of 2026-10-13: the base was prepared first, for the Owner side to read it,
    and the ladder woke the Contractor 900 s later, while the Owner side was still writing the
    contract. The Contractor's task waits until the contract in force names an acceptance test
    and no change of it awaits a decision (US-E7-01)."""
    base_prepared(env, env.project_a)
    # the seed contract has stories and criteria, but no criterion names its test yet
    assert contractor_steps(env, after=86_400) == []
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    env.clock.advance(3600)
    # the acceptance tests are proposed, not decided: the contract is still a draft
    assert contractor_steps(env, after=86_400) == []
    assert env.call(
        "operator", "contract.approve", project_id=env.project_a, change_id=change_id
    ).ok
    # the ladder counts from the decision, not from the preparation of the base
    assert contractor_steps(env, after=899) == []
    assert contractor_steps(env, after=900) == ["wake"]


def test_a_change_proposed_during_the_task_holds_its_ladder_until_it_is_decided(
    env: Env,
) -> None:
    """The contract changes while the Contractor carries its task: no step is taken while the
    change awaits a decision, and the decision starts the ladder again, from the wake."""
    assert env.continuity is not None
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    assert env.call(
        "operator", "contract.approve", project_id=env.project_a, change_id=change_id
    ).ok
    base_prepared(env, env.project_a)
    (wake,) = env.continuity.due(env.clock.now() + timedelta(seconds=900))
    env.clock.advance(900)
    env.continuity.taken(wake, env.clock.now())  # the Contractor was woken, and did nothing
    change_id = propose(env, "main", env.project_a, note(7, "EXG-02"))
    assert contractor_steps(env, after=86_400) == []
    env.clock.advance(7200)
    assert env.call("owner_a", "contract.reject", project_id=env.project_a, change_id=change_id).ok
    assert contractor_steps(env, after=899) == []
    assert contractor_steps(env, after=900) == ["wake"]


def test_a_reserved_change_that_names_a_test_makes_the_contract_ready(env: Env) -> None:
    """The operator's own change counts like an approved one: the ladder starts from it."""
    base_prepared(env, env.project_a)
    env.clock.advance(3600)
    response = env.call(
        "operator",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="the acceptance test of EXG-01",
        operations=[wire(put(criterion_with_test("US-E1-01", "checks/add.txt::test_add")))],
    )
    assert response.ok, response.failure
    assert contractor_steps(env, after=899) == []
    assert contractor_steps(env, after=900) == ["wake"]


ONE_MORE_REQUIREMENT = """\
| ID | Requirement | Verification | Priority | Rationale |
| --- | --- | --- | --- | --- |
| EXG-03 | The system shall multiply. | T | P1 | Asked for |
"""


def test_an_import_releases_a_task_held_by_a_change_it_leaves_behind(env: Env) -> None:
    """contract.import writes a version too: a change proposed on the version before it can no
    longer be approved, so the Contractor's task goes on."""
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    assert env.call(
        "operator", "contract.approve", project_id=env.project_a, change_id=change_id
    ).ok
    base_prepared(env, env.project_a)
    propose(env, "main", env.project_a, note(7, "EXG-02"))
    assert contractor_steps(env, after=86_400) == []
    env.clock.advance(3600)
    imported = env.call(
        "operator",
        "contract.import",
        project_id=env.project_a,
        summary="one more requirement",
        markdown=ONE_MORE_REQUIREMENT,
    )
    assert imported.ok, imported.failure
    assert contractor_steps(env, after=899) == []
    assert contractor_steps(env, after=900) == ["wake"]


def delivery_task_log(env: Env, project: str) -> list[dict[str, object]]:
    """What the log says of the Contractor's task besides the steps of its ladder."""
    records = [
        json.loads(record.parameters)
        for record in env.audit.read(project)
        if record.action is Action.CONTINUITY and record.principal == "hub"
    ]
    return [record for record in records if record["step"] in ("hold", "release")]


def test_the_log_says_why_the_contractors_task_waits_and_when_it_goes_on(env: Env) -> None:
    """The operator tells a task held for want of a test from a ladder that stopped: each
    change of the reason is recorded, and the release, and nothing when nothing changes."""
    held = {"side": "contractor", "step": "hold", "task": "delivery"}
    base_prepared(env, env.project_a)
    assert delivery_task_log(env, env.project_a) == [{**held, "why": "no_acceptance_test"}]
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    other = propose(env, "main", env.project_a, note(7, "EXG-02"))
    assert env.call("operator", "contract.reject", project_id=env.project_a, change_id=other).ok
    assert delivery_task_log(env, env.project_a) == [
        {**held, "why": "no_acceptance_test"},
        {**held, "why": "pending_decision"},
    ]
    assert env.call(
        "operator", "contract.approve", project_id=env.project_a, change_id=change_id
    ).ok
    released = {"side": "contractor", "step": "release", "task": "delivery"}
    assert delivery_task_log(env, env.project_a)[2:] == [released]
    # a version that keeps the contract ready changes nothing of the task
    assert env.call(
        "operator",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="one more requirement",
        operations=[wire(put(requirement(4)))],
    ).ok
    assert delivery_task_log(env, env.project_a)[2:] == [released]


def test_an_approval_while_the_base_is_announced_is_not_lost(env: Env) -> None:
    """The delivery loop's thread reads the contract, then creates the Contractor's task; an
    approval on a listener's thread may land between the two. Both run under the continuity's
    lock: the approval waits, then finds the task and releases it (T12)."""
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    read_pending = env.contracts.pending
    approvals: list[threading.Thread] = []

    def pending_then_an_approval(principal: Principal, project_id: str) -> list[PendingChange]:
        found = read_pending(principal, project_id)
        if not approvals:  # once: the approval of the change just read as pending
            approval = threading.Thread(
                target=lambda: env.call(
                    "operator", "contract.approve", project_id=env.project_a, change_id=change_id
                )
            )
            approvals.append(approval)
            approval.start()
            approval.join(0.5)  # done by now, unless the lock holds it back
        return found

    env.contracts.pending = pending_then_an_approval  # type: ignore[method-assign]  # reason: the window between the read and the task
    base_prepared(env, env.project_a)
    approvals[0].join()
    assert env.contracts.read(OPERATOR, env.project_a).number == 2  # the change was approved
    assert contractor_steps(env, after=900) == ["wake"]


def test_a_hub_that_starts_again_acts_on_a_change_it_had_no_time_to_act_upon(env: Env) -> None:
    """The hub wrote a version, then stopped before it released the task the version made
    ready. At its next start the tasks it kept follow the contract as it is (T12)."""
    base_prepared(env, env.project_a)
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    env.contracts.approve(OPERATOR, env.project_a, change_id)  # written; nothing acted upon
    assert contractor_steps(env, after=86_400) == []
    env.hub.check_tasks()
    assert delivery_task_log(env, env.project_a)[-1]["step"] == "release"
    assert contractor_steps(env, after=900) == ["wake"]


def test_each_project_waits_for_its_own_contract(env: Env) -> None:
    """Two projects, two ladders: the contract of one moves only its own Contractor."""
    seed_contract(env, env.project_b)
    base_prepared(env, env.project_a)
    base_prepared(env, env.project_b)
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    assert env.call(
        "operator", "contract.approve", project_id=env.project_a, change_id=change_id
    ).ok
    assert contractor_steps(env, after=900) == ["wake"]
    assert contractor_steps(env, after=86_400, project=env.project_b) == []
    # a change of project B holds nothing of project A
    propose(env, "owner_b", env.project_b, criterion_with_test())
    assert contractor_steps(env, after=900) == ["wake"]
    assert contractor_steps(env, after=86_400, project=env.project_b) == []


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
    assert env.notifications == []  # queued: the delivery loop does not wait for the receiver
    assert env.send_notifications() == [
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


def documented_events(prefix: str) -> list[str]:
    """The events the table of docs/notifications.md documents, by name."""
    text = (Path(__file__).parents[3] / "docs" / "notifications.md").read_text(encoding="utf-8")
    return re.findall(rf"^\| `({re.escape(prefix)}[a-z_.]+)` \|", text, flags=re.MULTILINE)


def test_every_documented_event_of_the_delivery_loop_reaches_the_main_agent(env: Env) -> None:
    """T03 (I1): `delivery.reviewed` was documented and emitted, but never sent. The events
    relayed are the documented ones, no more, no fewer."""
    documented = documented_events("delivery.")
    assert sorted(documented) == sorted(NOTIFIED_EVENTS)
    for event in documented:
        env.hub.notified(event, {"project_id": env.project_a, "delivery_id": "d1"})
    env.hub.notified("staging.prepared", {"project_id": env.project_a})
    assert [event for event, _payload in env.send_notifications()] == documented


def test_a_refused_notification_is_logged_with_its_reason(env: Env) -> None:
    """EXG-48: the hub refused the host before sending; the log says so, with the code."""
    env.refuse_notifications.append("elsewhere.example")
    env.hub.notified(
        "delivery.decided",
        {"project_id": env.project_a, "delivery_id": "d_0001", "decision": "PASS"},
    )
    env.send_notifications()
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
    assert simulated.send_notifications()[-1][1]["simulation"] is True


def test_the_owner_learns_the_shape_of_a_change_from_the_tool_itself() -> None:
    """Hermes does not read MCP instructions: the second real run's Owner guessed the shape of
    an operation 22 times. The description and the field say it."""
    spec = TOOL_SPECS["contract.propose"]
    operations = next(field for field in spec.inputs.fields if field.name == "operations")
    assert '"op": "put", "object"' in operations.description
    assert '"op": "delete", "id"' in operations.description
    for shape in ("US-E1-01.1", "LNK:US-E1-01>EXG-01", "operator-reserved"):
        assert shape in spec.description


# ----------------------------------------------------------------------------- C49
def test_acceptance_put_names_the_criteria_that_use_the_file(env: Env) -> None:
    """C49: a file runs only through the criteria that name it."""
    attach_deliveries(env)
    named = {
        "type": "criterion",
        "id": "US-E1-01.2",
        "story": "US-E1-01",
        "text": "The system shall add.",
        "verification": "T",
        "test": "checks/extra.txt::test_extra",
    }
    applied = env.call(
        "operator",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="a criterion that names a test",
        operations=[wire(put(named))],
    )
    assert applied.ok, applied.failure
    response = env.call(
        "owner_a", "acceptance.put", path="checks/extra.txt", content="test_extra: pass\n"
    )
    assert response.ok, response.failure
    assert response.data is not None
    assert response.data["named_by"] == ["US-E1-01.2"]
    assert "warning" not in response.data


def test_acceptance_put_warns_when_no_criterion_names_the_file(env: Env) -> None:
    """C49: the Owner side's file that no criterion names never runs — the tool says so."""
    attach_deliveries(env)
    response = env.call(
        "owner_a", "acceptance.put", path="checks/orphan.txt", content="test_x: pass\n"
    )
    assert response.ok, response.failure
    assert response.data is not None
    assert response.data["named_by"] == []
    assert response.data["warning"] == _NOT_RUN


_NOT_RUN = (
    "no criterion of the current contract names this file in a story that covers a requirement "
    "or a constraint: the gate does not run it"
)


def test_acceptance_put_warns_when_the_story_of_the_criterion_covers_nothing(env: Env) -> None:
    """C49: the gate runs only the criteria of a story that covers a requirement or a constraint;
    a criterion of a story that covers nothing names a file that never runs."""
    attach_deliveries(env)
    loose = {
        "type": "criterion",
        "id": "US-E3-01.1",
        "story": "US-E3-01",
        "text": "The system shall add.",
        "verification": "T",
        "test": "checks/loose.txt::test_loose",
    }
    proposed = env.call(
        "owner_a",
        "contract.propose",
        summary="a story that covers nothing",
        operations=[wire(put(story(3, 1))), wire(put(loose))],
    )
    assert proposed.ok, proposed.failure
    assert proposed.data is not None
    approved = env.call(
        "operator",
        "contract.approve",
        project_id=env.project_a,
        change_id=proposed.data["change_id"],
    )
    assert approved.ok, approved.failure
    response = env.call(
        "owner_a", "acceptance.put", path="checks/loose.txt", content="test_loose: pass\n"
    )
    assert response.ok, response.failure
    assert response.data is not None
    assert response.data["named_by"] == []
    assert response.data["warning"] == _NOT_RUN


def test_acceptance_put_counts_a_pending_change_that_names_the_file(env: Env) -> None:
    """The third run: the Owner side proposed the criteria that name its tests, and put the
    tests before the approval. A file named by a change awaiting a decision is not one the gate
    never runs (audit of A2, C49 point 4)."""
    attach_deliveries(env)
    change_id = propose(env, "owner_a", env.project_a, criterion_with_test())
    response = env.call("owner_a", "acceptance.put", path="checks/sub.txt", content="t: pass\n")
    assert response.ok, response.failure
    assert response.data is not None
    assert response.data["named_by"] == []
    assert response.data["named_by_pending"] == {change_id: ["US-E1-02.2"]}
    warning = str(response.data["warning"])
    assert "does not run it" not in warning
    assert f"once change {change_id} is approved" in warning
    # approved, the change is the contract: the file is named, nothing to warn of
    assert env.call(
        "operator", "contract.approve", project_id=env.project_a, change_id=change_id
    ).ok
    again = env.call("owner_a", "acceptance.put", path="checks/sub.txt", content="t: pass\n")
    assert again.data is not None
    assert (again.data["named_by"], again.data["named_by_pending"]) == (["US-E1-02.2"], {})
    assert "warning" not in again.data


def test_acceptance_put_ignores_a_change_that_can_no_longer_be_approved(env: Env) -> None:
    """A change proposed on an older version cannot be approved: it names nothing that will
    run."""
    attach_deliveries(env)
    propose(env, "owner_a", env.project_a, criterion_with_test())
    assert env.call(
        "operator",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="one more requirement",
        operations=[wire(put(requirement(4)))],
    ).ok
    response = env.call("owner_a", "acceptance.put", path="checks/sub.txt", content="t: pass\n")
    assert response.data is not None
    assert response.data["named_by_pending"] == {}
    assert response.data["warning"] == _NOT_RUN


@pytest.mark.parametrize(
    ("tool", "field", "extra"),
    [
        ("instance.start", "prompt", {"side": "contractor"}),
        ("delivery.review", "reasons", {"delivery_id": "d1", "verdict": "reject"}),
    ],
)
def test_a_text_over_its_limit_is_refused_with_the_field_the_limit_and_the_length(
    env: Env, tool: str, field: str, extra: dict[str, str]
) -> None:
    """AN-022: a relaunch and the reasons of a verdict were refused at 4 000 characters, and the
    refusal did not say how long the text was."""
    response = env.call("operator", tool, project_id=env.project_a, **extra, **{field: "x" * 4321})
    assert response.failure is not None
    assert response.failure.reason is Reason.INVALID_REQUEST
    assert response.failure.message == (
        f"arguments.{field}: must have at most 4000 characters; it has 4321"
    )
    assert env.instances.prompts == []


def test_the_page_of_the_tools_gives_the_limit_of_every_prompt_and_of_the_reasons() -> None:
    page = (Path(__file__).parents[3] / "docs" / "tools.md").read_text(encoding="utf-8")
    assert "| `instance.start` | `prompt` | 4 000 |" in page
    assert "| `delivery.review` | `reasons` | 4 000 |" in page
