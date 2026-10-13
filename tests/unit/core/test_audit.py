"""The audit log: chain, escaping, head, verification (EXG-19)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.core.audit import (
    GENESIS_HASH,
    HUB_STREAM,
    MAX_PARAMETERS_BYTES,
    MAX_STRING,
    Action,
    AuditEvent,
    AuditLog,
    AuditRecord,
    Head,
    Outcome,
    Reason,
    canonical_json,
    canonical_parameters,
    format_line,
    record_hash,
)

PROJECT = "p_demo"


def make() -> tuple[AuditLog, InMemoryLogStore, FakeClock]:
    store = InMemoryLogStore()
    clock = FakeClock()
    return AuditLog(store, clock), store, clock


def ok(**parameters: object) -> AuditEvent:
    return AuditEvent(PROJECT, "operator", Action.CONTRACT_READ, Outcome.OK, parameters=parameters)


def refused(reason: Reason = Reason.FORBIDDEN) -> AuditEvent:
    return AuditEvent(
        PROJECT, "contractor:p_demo", Action.CONTRACT_PROPOSE, Outcome.REFUSED, reason
    )


def test_records_form_a_chain_from_a_fixed_start() -> None:
    log, _store, clock = make()
    first = log.append(ok(version=1))
    clock.advance(5)
    second = log.append(refused())
    assert (first.seq, second.seq) == (1, 2)
    assert first.previous == GENESIS_HASH
    assert second.previous == first.hash
    assert first.time == "2026-10-10T12:00:00.000000Z"
    assert second.time == "2026-10-10T12:00:05.000000Z"
    assert first.hash == record_hash(first)
    assert second.principal == "contractor:p_demo"
    assert second.reason is Reason.FORBIDDEN


def test_a_stream_has_its_own_chain() -> None:
    log, _store, _clock = make()
    log.append(ok())
    other = log.append(AuditEvent(HUB_STREAM, "operator", Action.PROJECT_CREATE, Outcome.OK))
    assert other.seq == 1
    assert other.previous == GENESIS_HASH
    assert log.streams() == [HUB_STREAM, PROJECT]


def test_a_new_log_verifies_and_so_does_a_log_after_many_appends() -> None:
    log, _store, _clock = make()
    assert log.verify(PROJECT).ok
    for number in range(25):
        log.append(ok(number=number))
    report = log.verify(PROJECT)
    assert (report.ok, report.records, report.first_bad_seq) == (True, 25, None)


def test_the_event_refuses_a_forged_stream_principal_or_reason() -> None:
    with pytest.raises(ValueError, match="stream"):
        AuditEvent("p\nfake", "operator", Action.WHOAMI, Outcome.OK)
    with pytest.raises(ValueError, match="principal"):
        AuditEvent(PROJECT, "operator\nseq=9", Action.WHOAMI, Outcome.OK)
    with pytest.raises(ValueError, match="states its reason"):
        AuditEvent(PROJECT, "operator", Action.WHOAMI, Outcome.REFUSED)
    with pytest.raises(ValueError, match="only a refusal"):
        AuditEvent(PROJECT, "operator", Action.WHOAMI, Outcome.OK, Reason.FORBIDDEN)


def test_the_action_is_a_closed_set() -> None:
    assert Action("contract.approve") is Action.CONTRACT_APPROVE
    with pytest.raises(ValueError, match="is not a valid Action"):
        Action("contract.approve\nseq=999")


# ----------------------------------------------------------------------------- tampering
def tamper(store: InMemoryLogStore, seq: int, **changes: Any) -> None:
    records = store._records[PROJECT]  # reason: the test plays the attacker who owns the store
    records[seq - 1] = dataclasses.replace(records[seq - 1], **changes)


@pytest.fixture
def filled() -> tuple[AuditLog, InMemoryLogStore]:
    log, store, clock = make()
    for number in range(6):
        clock.advance(1)
        log.append(ok(number=number))
    return log, store


@pytest.mark.parametrize(
    ("changes", "seq"),
    [
        ({"principal": "operator2"}, 3),
        ({"parameters": '{"number":99}'}, 4),
        ({"outcome": Outcome.ERROR}, 2),
        ({"time": "2020-01-01T00:00:00.000000Z"}, 5),
        ({"action": Action.UNKNOWN}, 1),
        ({"hash": "f" * 64}, 6),
        ({"previous": "e" * 64}, 3),
    ],
)
def test_a_changed_field_is_found_at_its_record(
    filled: tuple[AuditLog, InMemoryLogStore], changes: dict[str, Any], seq: int
) -> None:
    log, store = filled
    tamper(store, seq, **changes)
    report = log.verify(PROJECT)
    assert not report.ok
    assert report.first_bad_seq == seq


def test_a_deleted_middle_record_is_found() -> None:
    log, store, _clock = make()
    for number in range(5):
        log.append(ok(number=number))
    del store._records[PROJECT][2]  # reason: the test plays the attacker
    report = log.verify(PROJECT)
    assert (report.ok, report.first_bad_seq, report.problem) == (False, 3, "the sequence is broken")


def test_two_swapped_records_are_found() -> None:
    log, store, _clock = make()
    for number in range(5):
        log.append(ok(number=number))
    records = store._records[PROJECT]  # reason: the test plays the attacker
    records[1], records[2] = records[2], records[1]
    assert log.verify(PROJECT).first_bad_seq == 2


def test_a_truncated_tail_is_found_through_the_head() -> None:
    log, store, _clock = make()
    for number in range(5):
        log.append(ok(number=number))
    del store._records[PROJECT][3:]  # reason: the test plays the attacker
    report = log.verify(PROJECT)
    assert not report.ok
    assert report.problem == "the last record does not match the head"


def test_a_log_emptied_behind_its_head_is_found() -> None:
    log, store, _clock = make()
    log.append(ok())
    store._records[PROJECT].clear()  # reason: the test plays the attacker
    assert not log.verify(PROJECT).ok


def test_a_missing_head_is_found() -> None:
    log, store, _clock = make()
    log.append(ok())
    del store._heads[PROJECT]  # reason: the test plays the attacker
    assert not log.verify(PROJECT).ok


def test_a_record_of_another_stream_is_found() -> None:
    log, store, _clock = make()
    log.append(ok())
    tamper(store, 1, stream="p_other")
    report = log.verify(PROJECT)
    assert report.problem == "a record of another stream"


def test_a_forged_head_is_found() -> None:
    log, store, _clock = make()
    record = log.append(ok())
    store.set_head(Head(PROJECT, record.seq, "0" * 64))
    assert not log.verify(PROJECT).ok


def test_a_rechained_forgery_passes_the_chain_and_not_the_head() -> None:
    """A whole stream replaced by a consistent forgery is caught by the head, which an
    attacker with the store would also replace: the external anchor is Z3."""
    log, store, _clock = make()
    for number in range(3):
        log.append(ok(number=number))
    old_head = store.head(PROJECT)
    forged_store = InMemoryLogStore()
    forged_log = AuditLog(forged_store, FakeClock())
    for number in range(3):
        forged_log.append(ok(number=number + 100))
    store._records[PROJECT] = forged_store._records[PROJECT]  # reason: the test plays the attacker
    assert store.head(PROJECT) == old_head
    assert not log.verify(PROJECT).ok


# ----------------------------------------------------------------------------- ATK-70
HOSTILE = [
    "line one\nline two",
    "carriage\rreturn",
    "tab\tand\x00nul\x1b[31mred",
    "separators " + chr(0x2028) + " " + chr(0x2029) + " \x85",
    '{"seq": 999, "action": "contract.approve", "outcome": "ok"}',
    '"}\n{"seq":2,"principal":"operator"',
    "2026-10-10T12:00:00Z operator contract.approve ok",
    "‮ right-to-left override",
    "emoji \U0001f600 and \udcff lone surrogate",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_value_leaves_one_record_and_one_line(value: str) -> None:
    log, _store, _clock = make()
    record = log.append(ok(note=value, nested={"deep": [value]}))
    line = format_line(record)
    assert "\n" not in line
    assert "\r" not in line
    assert len(line.splitlines()) == 1
    assert len(log.read(PROJECT)) == 1
    assert (
        json.loads(line)["parameters"]["note"] == value[:MAX_STRING]
        or value[:MAX_STRING] in json.loads(line)["parameters"]["note"]
    )
    assert log.verify(PROJECT).ok
    assert line.isascii()


@settings(max_examples=150, deadline=None)
@given(
    text=st.text(min_size=0, max_size=300),
    key=st.text(min_size=0, max_size=40),
    count=st.integers(min_value=1, max_value=4),
)
def test_any_text_gives_one_line_per_record(text: str, key: str, count: int) -> None:
    log, _store, _clock = make()
    for _ in range(count):
        log.append(ok(**{key: text, "list": [text, {"k": text}]}))
    lines = [format_line(record) for record in log.read(PROJECT)]
    assert len(lines) == count
    joined = "\n".join(lines)
    assert len(joined.splitlines()) == count
    assert joined.isascii()
    assert log.verify(PROJECT).ok


def test_an_unknown_action_name_goes_in_the_parameters_not_in_the_action() -> None:
    log, _store, _clock = make()
    record = log.append(
        AuditEvent(
            PROJECT,
            "contractor:p_demo",
            Action.UNKNOWN,
            Outcome.REFUSED,
            Reason.UNKNOWN_ACTION,
            {"requested": "log.delete\nseq=1"},
        )
    )
    assert record.action is Action.UNKNOWN
    assert json.loads(record.parameters) == {"requested": "log.delete\nseq=1"}
    assert format_line(record).count("\n") == 0


# ----------------------------------------------------------------------------- bounds
def test_long_strings_deep_values_and_odd_types_are_bounded() -> None:
    text = canonical_parameters({"long": "x" * (MAX_STRING + 40)})
    assert json.loads(text)["long"].endswith("...[40 more characters]")
    deep: dict[str, object] = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    assert "[too deep]" in canonical_parameters(deep)
    odd = canonical_parameters(
        {"f": float("nan"), "i": float("inf"), "s": {1, 2}, "t": (1, 2), "n": None, "b": True}
    )
    assert json.loads(odd) == {
        "f": "nan",
        "i": "inf",
        "s": "[set]",
        "t": [1, 2],
        "n": None,
        "b": True,
    }
    assert json.loads(canonical_parameters({"x": 1.5})) == {"x": 1.5}
    assert canonical_parameters(None) == "{}"


def test_huge_parameters_are_replaced_by_a_marker() -> None:
    big = {str(number): "y" * 400 for number in range(40)}
    text = canonical_parameters(big)
    assert len(text) < MAX_PARAMETERS_BYTES
    assert json.loads(text)["truncated"] is True


def test_canonical_json_is_deterministic_and_ascii() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"\\u00e9","b":1}'


def test_a_record_is_immutable() -> None:
    log, _store, _clock = make()
    record = log.append(ok())
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.seq = 5  # type: ignore[misc]  # reason: the test checks that a record is frozen
    assert isinstance(record, AuditRecord)
