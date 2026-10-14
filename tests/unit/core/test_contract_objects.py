"""Contract objects and changes: closed fields, bounded values (US-E2-01)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.unit.core.contract_fixtures import (
    constraint,
    criterion,
    link,
    note,
    requirement,
    story,
)
from zephyros.core.contract import (
    CONTRACT_SCHEMA_VERSION,
    MAX_OPERATIONS,
    ContractError,
    Operation,
    apply_operations,
    check_integrity,
    content_hash,
    empty_version,
    operations_from_data,
    operations_to_data,
    p0_criteria,
    parse_change,
    reserved_view,
    validate_object,
    version_id,
)

VALID = [
    requirement(1),
    constraint(1),
    story(1, 1),
    criterion(1, 1, 1),
    link(1, 1, "EXG-01"),
    note(1, "EXG-01"),
]


@pytest.mark.parametrize("obj", VALID, ids=lambda o: o["type"])
def test_a_valid_object_conforms(obj: dict[str, Any]) -> None:
    assert validate_object(obj) == []


@pytest.mark.parametrize("obj", VALID, ids=lambda o: o["type"])
def test_a_missing_field_an_extra_field_or_a_wrong_type_is_refused(obj: dict[str, Any]) -> None:
    for name in obj:
        broken = copy.deepcopy(obj)
        del broken[name]
        if name == "verification":
            assert validate_object(broken) == []
        else:
            assert validate_object(broken), name
    extra = {**obj, "surprise": "x"}
    assert [p.message for p in validate_object(extra)] == ["unknown key"]
    wrong = {**obj, "text": 5} if "text" in obj else {**obj, "id": 5}
    assert validate_object(wrong)


def test_a_requirement_needs_one_or_two_known_methods() -> None:
    """US-E2-01: a list of one to two verification methods (T, I, A, D)."""
    for methods, ok in [
        (["test"], True),
        (["test", "analysis"], True),
        (["inspection", "demonstration"], True),
        ([], False),
        (["test", "analysis", "inspection"], False),
        (["test", "test"], False),
        (["magic"], False),
        (["Test"], False),
        ("test", False),
        (None, False),
        ([["test"]], False),
    ]:
        problems = validate_object({**requirement(1), "methods": methods})
        assert (problems == []) is ok, methods
        if not ok:
            assert any(p.key.startswith("object.methods") for p in problems)


@pytest.mark.parametrize("priority", ["P4", "p0", "", "high", 0, None])
def test_a_priority_is_one_of_four(priority: object) -> None:
    assert validate_object(requirement(1, priority=priority))  # type: ignore[arg-type]  # reason: invalid on purpose


@pytest.mark.parametrize(
    "bad_id",
    ["EXG-1", "EXG-1000", "exg-01", "EXG-01\n", "CTR-01", "EXG-01 ", "../EXG-01", "EXG-0a"],
)
def test_identifiers_follow_their_pattern(bad_id: str) -> None:
    obj = requirement(1)
    obj["id"] = bad_id
    assert validate_object(obj)


def test_text_may_span_lines_but_holds_no_other_control_character() -> None:
    assert validate_object(requirement(1, text="line one\nline two\ttab")) == []
    for bad in ("nul\x00", "esc\x1b[31m", "bell\x07", "\x85next"):
        assert validate_object(requirement(1, text=bad)), repr(bad)
    assert validate_object(requirement(1, text=""))
    assert validate_object(requirement(1, text="x" * 4001))


def test_an_object_has_a_known_type() -> None:
    assert validate_object({"id": "EXG-01"})[0].key == "object.type"
    assert validate_object({"type": "tool", "id": "x"})[0].key == "object.type"
    assert validate_object("EXG-01")[0].message == "must be an object"
    assert validate_object({"type": ["requirement"]})


# ----------------------------------------------------------------------------- changes
def body(*operations: object, summary: object = "why") -> dict[str, Any]:
    return {"summary": summary, "operations": list(operations)}


def put_op(obj: object) -> dict[str, object]:
    return {"op": "put", "object": obj}


def test_a_change_is_parsed_into_operations() -> None:
    change = parse_change(body(put_op(requirement(1)), {"op": "delete", "id": "EXG-02"}))
    assert [o.op for o in change.operations] == ["put", "delete"]
    assert change.operations[1].id == "EXG-02"
    assert change.summary == "why"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"summary": "x"},
        {"operations": [put_op(requirement(1))]},
        body(summary=""),
        body(put_op(requirement(1)), summary=5),
        body(put_op(requirement(1)), summary="x" * 301),
        {**body(put_op(requirement(1))), "author": "owner"},
        {**body(put_op(requirement(1))), "project": "p_other"},
        body({"op": "drop", "id": "EXG-01"}),
        body({"op": "put"}),
        body({"op": "put", "object": "text"}),
        body({"op": "delete"}),
        body({"op": "delete", "id": "EXG-01", "force": True}),
        body({"op": "put", "object": requirement(1), "extra": 1}),
        body("put EXG-01"),
        body(put_op({**requirement(1), "methods": []})),
        body(put_op({**requirement(1), "$where": "1"})),
    ],
)
def test_a_malformed_change_is_refused_with_its_faults(bad: object) -> None:
    with pytest.raises(ContractError) as caught:
        parse_change(bad)
    assert caught.value.problems


def test_a_change_of_too_many_operations_is_refused() -> None:
    operations = [
        {"op": "delete", "id": f"EXG-{n % 99 + 1:02d}"} for n in range(MAX_OPERATIONS + 1)
    ]
    with pytest.raises(ContractError):
        parse_change(body(*operations))


def test_unknown_fields_are_named() -> None:
    with pytest.raises(ContractError) as caught:
        parse_change({**body(put_op(requirement(1))), "author": "owner"})
    assert [p.key for p in caught.value.problems] == ["change.author"]


# ----------------------------------------------------------------------------- applying
def test_operations_apply_to_a_copy_and_keep_references_whole() -> None:
    base: dict[str, dict[str, object]] = {}
    result = apply_operations(
        base, [Operation("put", object=requirement(1)), Operation("put", object=story(1, 1))]
    )
    assert base == {}
    assert set(result) == {"EXG-01", "US-E1-01"}
    linked = apply_operations(result, [Operation("put", object=link(1, 1, "EXG-01"))])
    assert "LNK:US-E1-01>EXG-01" in linked


@pytest.mark.parametrize(
    "operations",
    [
        [Operation("delete", id="EXG-99")],
        [Operation("put", object=link(1, 1, "EXG-09"))],  # no such requirement
        [Operation("put", object=criterion(1, 9, 1))],  # no such story
        [Operation("put", object=note(1, "EXG-77"))],  # no target
        [Operation("put", object={**link(1, 1, "EXG-01"), "id": "LNK:US-E1-01>EXG-02"})],
        [Operation("put", object={**criterion(1, 1, 1), "id": "US-E1-02.1"})],
        [Operation("delete")],
    ],
)
def test_a_change_that_breaks_a_reference_is_refused(operations: list[Operation]) -> None:
    base = apply_operations(
        {},
        [
            Operation("put", object=requirement(1)),
            Operation("put", object=requirement(2)),
            Operation("put", object=story(1, 1)),
            Operation("put", object=story(1, 2)),
        ],
    )
    with pytest.raises(ContractError):
        apply_operations(base, operations)


def test_deleting_a_requirement_that_a_link_names_is_refused_until_the_link_goes() -> None:
    base = apply_operations(
        {},
        [
            Operation("put", object=requirement(1)),
            Operation("put", object=story(1, 1)),
            Operation("put", object=link(1, 1, "EXG-01")),
        ],
    )
    with pytest.raises(ContractError):
        apply_operations(base, [Operation("delete", id="EXG-01")])
    cleaned = apply_operations(
        base, [Operation("delete", id="LNK:US-E1-01>EXG-01"), Operation("delete", id="EXG-01")]
    )
    assert "EXG-01" not in cleaned


def test_integrity_checks_the_key_against_the_identifier() -> None:
    problems = check_integrity({"EXG-02": requirement(1)})
    assert problems[0].message == "the identifier differs from the key"


def test_operations_round_trip_through_data() -> None:
    operations = (Operation("put", object=requirement(1)), Operation("delete", id="EXG-02"))
    assert operations_from_data(operations_to_data(operations)) == operations


# ----------------------------------------------------------------------------- views
def objects(*items: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def test_a_p0_criterion_is_one_of_a_story_that_covers_a_p0_requirement() -> None:
    world = objects(
        requirement(1, priority="P0"),
        requirement(2),
        story(1, 1),
        story(1, 2),
        link(1, 1, "EXG-01"),
        link(1, 2, "EXG-02"),
        criterion(1, 1, 1),
        criterion(1, 2, 1),
    )
    assert p0_criteria(world) == {"US-E1-01.1"}


def test_the_reserved_view_changes_with_exactly_what_the_operator_owns() -> None:
    world = objects(
        requirement(1, priority="P0"),
        requirement(2),
        story(1, 1),
        story(1, 2),
        link(1, 1, "EXG-01"),
        link(1, 2, "EXG-02"),
        criterion(1, 1, 1),
        criterion(1, 2, 1),
        note(1, "EXG-01"),
    )
    base = reserved_view(world)
    assert set(base) == {"EXG-01", "EXG-02", "LNK:US-E1-01>EXG-01", "US-E1-01.1"}

    def changed(**updates: dict[str, Any]) -> bool:
        return reserved_view({**world, **dict(updates)}) != base

    # In the view
    assert changed(**{"EXG-01": requirement(1, text="Another text.", priority="P0")})
    assert changed(**{"EXG-02": requirement(2, priority="P0")})
    assert changed(**{"EXG-02": requirement(2, methods=("analysis",))})
    assert changed(**{"US-E1-01.1": criterion(1, 1, 1, "Changed.")})
    # Not in the view
    assert not changed(**{"US-E1-01": story(1, 1, "A new title")})
    assert not changed(**{"US-E1-02.1": criterion(1, 2, 1, "Changed.")})
    assert not changed(**{"NB-001": note(1, "EXG-01", "Another note.")})
    assert (
        changed(**{"US-E1-01.2": criterion(1, 1, 2)}) is not False
    )  # a new P0 criterion is in the view


def test_version_identifiers_carry_a_counter_and_the_content_hash() -> None:
    digest = content_hash({})
    assert version_id(3, digest) == f"v3-{digest[:12]}"
    assert version_id(1, digest) != version_id(2, digest)
    assert content_hash({"a": {"x": 1}}) == content_hash({"a": {"x": 1}})
    assert content_hash({"a": {"x": 1}}) != content_hash({"a": {"x": 2}})
    assert (
        empty_version(
            __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC)
        ).number
        == 0
    )
    assert CONTRACT_SCHEMA_VERSION == "1.1.0"
