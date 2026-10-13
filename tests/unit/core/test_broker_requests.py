"""The requests the hub may send the filter: a closed set, a strict parser (EXG-34; ATK-73)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from zephyros.core.broker import (
    MAX_BODY,
    BrokerError,
    ContainerInfo,
    Event,
    Op,
    Request,
    event_from_json,
    event_to_json,
    info_from_json,
    info_to_json,
    parse_body,
    parse_request,
    request_to_json,
)
from zephyros.core.containers import Role, VolumeKind
from zephyros.core.ports.agent_engine import Side

PROJECT = "p_abcdef12"
TOKEN = "zph_o_" + "B" * 43
VALID: dict[str, dict[str, Any]] = {
    "network.create": {"project_id": PROJECT, "side": "owner", "subnet": "10.200.0.16/28"},
    "network.remove": {"project_id": PROJECT, "side": "owner"},
    "hub.connect": {"project_id": PROJECT, "side": "owner"},
    "hub.disconnect": {"project_id": PROJECT, "side": "contractor"},
    "volume.create": {"project_id": PROJECT, "side": "owner", "volume": "home"},
    "volume.remove": {"project_id": PROJECT, "side": "owner", "volume": "work"},
    "guard.start": {"project_id": PROJECT, "side": "owner", "subnet": "10.200.0.16/28"},
    "guard.stop": {"project_id": PROJECT, "side": "owner"},
    "turn.start": {"project_id": PROJECT, "side": "owner", "token": TOKEN, "prompt": "Work."},
    "turn.stop": {"project_id": PROJECT, "side": "owner"},
    "container.inspect": {"project_id": PROJECT, "side": "owner", "role": "guard"},
    "container.list": {},
    "events": {"since": 1_700_000_000},
}


def body(op: str) -> dict[str, Any]:
    return {"op": op, **VALID[op]}


def test_every_kind_has_a_valid_request() -> None:
    assert set(VALID) == {op.value for op in Op}
    for op in VALID:
        request = parse_request(body(op))
        assert request.op.value == op


def test_a_request_survives_the_round_trip_to_json() -> None:
    for op in VALID:
        request = parse_request(body(op))
        assert parse_request(json.loads(json.dumps(request_to_json(request)))) == request


def test_values_become_typed() -> None:
    request = parse_request(body("turn.start"))
    assert request.side is Side.OWNER
    assert request.token == TOKEN
    assert parse_request(body("volume.create")).volume is VolumeKind.HOME
    assert parse_request(body("container.inspect")).role is Role.GUARD
    assert "zph_" not in repr(request)  # a token and a prompt stay out of a repr


@pytest.mark.parametrize("op", sorted(VALID))
def test_a_missing_field_or_an_extra_field_is_refused(op: str) -> None:
    for name in VALID[op]:
        broken = body(op)
        del broken[name]
        with pytest.raises(BrokerError) as caught:
            parse_request(broken)
        assert caught.value.code == "bad_request"
    extra = {**body(op), "image": "evil"}
    with pytest.raises(BrokerError) as caught:
        parse_request(extra)
    assert "image" in str(caught.value)


RAW_API_SHAPES = [
    {"Image": "alpine", "Cmd": ["sh"]},
    {"Image": "alpine", "HostConfig": {"Privileged": True}},
    {"op": "containers/create", "Image": "alpine"},
    {"op": "exec", "container": "x", "Cmd": ["sh"]},
    {"op": "build"},
    {"op": "commit", "container": "x"},
    {"op": "copy", "container": "x", "path": "/"},
    {"op": "network.connect", "container": "other"},
    {"op": "container.remove", "name": "other"},
    {"op": "container.stop", "name": "other"},
    {"op": "image.pull", "image": "evil"},
    {"op": "network.create", "name": "mine", "driver": "host"},
    {"op": ["network.create"]},
    {"op": None},
    {"op": ""},
    {"op": "NETWORK.CREATE"},
    {},
    {"op": "turn.start ", "project_id": PROJECT},
]


@pytest.mark.parametrize("data", RAW_API_SHAPES)
def test_a_request_shaped_like_a_call_to_the_runtime_is_refused(data: dict[str, Any]) -> None:
    with pytest.raises(BrokerError):
        parse_request(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "p_x"),
        ("project_id", "--privileged"),
        ("project_id", "../.."),
        ("project_id", "p_abcdef12\n"),
        ("project_id", "$(id)"),
        ("project_id", 5),
        ("side", "both"),
        ("side", "OWNER"),
        ("side", ["owner"]),
        ("subnet", "10.200.0.16"),
        ("subnet", "10.200.0.16/28; id"),
        ("subnet", "::/0"),
        ("volume", "root"),
        ("role", "hub"),
        ("role", "network"),
        ("token", "sk-live-1234"),
        ("token", "zph_m_" + "A" * 43),
        ("token", "zph_o_short"),
        ("prompt", ""),
        ("prompt", "x" * 4001),
        ("prompt", 5),
        ("since", -1),
        ("since", "yesterday"),
        ("since", 2**41),
        ("since", True),
    ],
)
def test_a_value_outside_its_pattern_is_refused(field: str, value: object) -> None:
    for op, template in VALID.items():
        if field in template:
            broken = {**body(op), field: value}
            with pytest.raises(BrokerError):
                parse_request(broken)


# ----------------------------------------------------------------------------- the body
def test_the_body_is_one_json_object_without_a_repeated_key() -> None:
    assert parse_body(b'{"op": "container.list"}') == {"op": "container.list"}
    for raw, code in [
        (b'{"op": "a", "op": "b"}', "duplicate_key"),
        (b'{"a": {"b": 1, "b": 2}}', "duplicate_key"),
        (b"[1]", "bad_request"),
        (b'"text"', "bad_request"),
        (b"null", "bad_request"),
        (b"{not json", "bad_json"),
        (b"", "bad_json"),
        (b"\xff\xfe", "bad_json"),
        (b'{"a": NaN}', "bad_json"),
        (b'{"a": Infinity}', "bad_json"),
        (b"x" * (MAX_BODY + 1), "too_large"),
    ]:
        with pytest.raises(BrokerError) as caught:
            parse_body(raw)
        assert caught.value.code == code, raw


@settings(max_examples=300, deadline=None)
@given(
    data=st.recursive(
        st.none()
        | st.booleans()
        | st.integers()
        | st.floats(allow_nan=False)
        | st.text(max_size=40),
        lambda children: (
            st.lists(children, max_size=4)
            | st.dictionaries(st.text(max_size=12), children, max_size=5)
        ),
        max_leaves=15,
    )
)
def test_arbitrary_json_is_either_a_valid_request_or_a_broker_error(data: object) -> None:
    try:
        raw = json.dumps(data).encode()
    except ValueError:
        return
    try:
        request = parse_request(parse_body(raw))
    except BrokerError:
        return
    assert isinstance(request, Request)
    assert request.op in set(Op)


@settings(max_examples=200, deadline=None)
@given(
    op=st.sampled_from(sorted(VALID)),
    key=st.text(min_size=1, max_size=20),
    value=st.one_of(st.text(max_size=30), st.integers(), st.none(), st.booleans()),
)
def test_adding_any_field_to_a_valid_request_refuses_it(op: str, key: str, value: object) -> None:
    data = body(op)
    if key in data:
        return
    with pytest.raises(BrokerError):
        parse_request({**data, key: value})


def test_container_information_and_events_round_trip() -> None:
    info = ContainerInfo(
        "guard", PROJECT, "owner", "running", "healthy", None, "10.200.0.18", "a", None
    )
    assert info_from_json(info_to_json(info)) == info
    exited = ContainerInfo("turn", PROJECT, "owner", "exited", None, 3, None, "a", "b")
    assert info_from_json(info_to_json(exited)) == exited
    event = Event(5, "die", "turn", PROJECT, "owner")
    assert event_from_json(event_to_json(event)) == event
