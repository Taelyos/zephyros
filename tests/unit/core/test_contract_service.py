"""The contract service: propose, approve, reject, and the floor of the operator.

Covers US-E2-01, US-E2-02, and the hub side of ATK-09, ATK-10, ATK-88, ATK-89.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.unit.core.contract_fixtures import (
    ALL_PRINCIPALS,
    CONTRACTOR,
    FOREIGN_OWNER,
    OTHER_PROJECT,
    OWNER,
    OWNER_TWO,
    PROJECT,
    change,
    constraint,
    criterion,
    delete,
    link,
    make_service,
    note,
    put,
    requirement,
    seed_operations,
    seeded,
    story,
)
from zephyros.core.audit import Reason
from zephyros.core.contract import (
    ChangeStatus,
    ContractService,
    Operation,
    check_floor,
    reserved_view,
)
from zephyros.core.errors import Refusal
from zephyros.core.principals import MAIN_AGENT, OPERATOR, Principal


def refusal_reason(call, *args) -> Reason:  # type: ignore[no-untyped-def]  # reason: a small helper for the tests
    with pytest.raises(Refusal) as caught:
        call(*args)
    return caught.value.reason


# ----------------------------------------------------------------------------- the operator
def test_the_operator_imports_the_contract_as_the_first_version() -> None:
    service, store, _clock = make_service()
    version = service.apply_reserved(OPERATOR, PROJECT, change(*seed_operations()))
    assert version.number == 1
    assert version.parent is None
    assert version.operator_written
    assert version.author == "operator"
    assert version.id.startswith("v1-")
    assert len(version.objects) == len(seed_operations())
    assert service.read(OPERATOR, PROJECT).id == version.id
    assert store.numbers(PROJECT) == [1]


def test_only_the_operator_applies_reserved_data() -> None:
    service, _store, _clock = seeded()
    edit = change(put(requirement(1, text="Another text.", priority="P0")))
    for principal in (MAIN_AGENT, OWNER, OWNER_TWO, CONTRACTOR, FOREIGN_OWNER):
        assert refusal_reason(service.apply_reserved, principal, PROJECT, edit) is Reason.FORBIDDEN
    assert service.read(OPERATOR, PROJECT).number == 1


def test_apply_reserved_refuses_a_change_that_touches_nothing_reserved() -> None:
    service, _store, _clock = seeded()
    assert (
        refusal_reason(service.apply_reserved, OPERATOR, PROJECT, change(put(note(2, "EXG-01"))))
        is Reason.INVALID_REQUEST
    )


def test_apply_reserved_refuses_a_change_that_breaks_the_contract() -> None:
    service, _store, _clock = seeded()
    assert (
        refusal_reason(service.apply_reserved, OPERATOR, PROJECT, change(delete("EXG-01")))
        is Reason.INVALID_REQUEST
    )


# ----------------------------------------------------------------------------- US-E2-02
def propose(service: ContractService, principal: Principal, *operations: Operation) -> str:
    pending = service.propose(principal, PROJECT, change(*operations))
    return str(pending.id)


def test_a_proposal_is_recorded_as_pending_and_the_version_stays() -> None:
    service, _store, _clock = seeded()
    before = service.read(OPERATOR, PROJECT)
    pending = service.propose(OWNER, PROJECT, change(put(note(2, "EXG-02")), summary="add a note"))
    assert pending.status is ChangeStatus.PENDING
    assert pending.proposer == OWNER.id
    assert pending.base_version == 1
    assert [c.id for c in service.pending(OPERATOR, PROJECT)] == [pending.id]
    assert service.read(OPERATOR, PROJECT).id == before.id
    assert "NB-002" not in service.read(OPERATOR, PROJECT).objects


def test_another_principal_approves_and_a_new_version_appears() -> None:
    service, _store, clock = seeded()
    first = service.read(OPERATOR, PROJECT)
    change_id = propose(service, OWNER, put(note(2, "EXG-02")))
    clock.advance(10)
    version = service.approve(MAIN_AGENT, PROJECT, change_id)
    assert version.number == 2
    assert version.parent == first.id
    assert version.id != first.id
    assert version.author == "main-agent"
    assert not version.operator_written
    assert "NB-002" in version.objects
    assert service.pending(OPERATOR, PROJECT) == []
    # Positive control of ATK-89: another Owner principal approves an Owner's proposal.
    second = propose(service, OWNER, put(note(3, "EXG-02")))
    assert service.approve(OWNER_TWO, PROJECT, second).number == 3
    assert check_floor([service.read(OPERATOR, PROJECT, n) for n in (1, 2, 3)]) == []


def test_a_rejection_keeps_the_current_version() -> None:
    service, _store, _clock = seeded()
    before = service.read(OPERATOR, PROJECT)
    change_id = propose(service, OWNER, put(note(2, "EXG-02")))
    resolved = service.reject(MAIN_AGENT, PROJECT, change_id)
    assert resolved.status is ChangeStatus.REJECTED
    assert resolved.resolved_by == "main-agent"
    assert service.read(OPERATOR, PROJECT).id == before.id
    assert service.pending(OPERATOR, PROJECT) == []
    assert refusal_reason(service.approve, MAIN_AGENT, PROJECT, change_id) is Reason.NOT_PENDING


def test_the_proposer_cannot_approve_its_own_change() -> None:
    """ATK-89: the same token, a rotated token, a restarted instance: one stable principal."""
    service, _store, _clock = seeded()
    change_id = propose(service, OWNER, put(note(2, "EXG-02")))
    assert refusal_reason(service.approve, OWNER, PROJECT, change_id) is Reason.SELF_APPROVAL
    restarted = Principal(OWNER.id, OWNER.kind, OWNER.project_id, OWNER.instance_name)
    assert refusal_reason(service.approve, restarted, PROJECT, change_id) is Reason.SELF_APPROVAL
    assert [c.id for c in service.pending(OPERATOR, PROJECT)] == [change_id]
    assert service.read(OPERATOR, PROJECT).number == 1


def test_the_proposer_may_not_approve_even_as_the_main_agent_or_operator() -> None:
    service, _store, _clock = seeded()
    for proposer in (MAIN_AGENT, OPERATOR):
        change_id = propose(service, proposer, put(note(9, "EXG-02")))
        assert refusal_reason(service.approve, proposer, PROJECT, change_id) is Reason.SELF_APPROVAL
        service.reject(OWNER, PROJECT, change_id)


def test_the_contractor_and_a_foreign_owner_cannot_propose_or_approve() -> None:
    service, _store, _clock = seeded()
    change_id = propose(service, OWNER, put(note(2, "EXG-02")))
    assert (
        refusal_reason(service.propose, CONTRACTOR, PROJECT, change(put(note(5, "EXG-02"))))
        is Reason.FORBIDDEN
    )
    assert refusal_reason(service.approve, CONTRACTOR, PROJECT, change_id) is Reason.FORBIDDEN
    assert refusal_reason(service.reject, CONTRACTOR, PROJECT, change_id) is Reason.FORBIDDEN
    assert (
        refusal_reason(service.approve, FOREIGN_OWNER, PROJECT, change_id) is Reason.OTHER_PROJECT
    )
    assert (
        refusal_reason(service.propose, FOREIGN_OWNER, PROJECT, change(put(note(5, "EXG-02"))))
        is Reason.OTHER_PROJECT
    )
    assert service.read(OPERATOR, PROJECT).number == 1


def test_a_change_cannot_be_approved_twice_nor_after_the_contract_moved() -> None:
    service, _store, _clock = seeded()
    first = propose(service, OWNER, put(note(2, "EXG-02")))
    second = propose(service, OWNER, put(note(3, "EXG-02")))
    service.approve(MAIN_AGENT, PROJECT, first)
    assert refusal_reason(service.approve, MAIN_AGENT, PROJECT, first) is Reason.NOT_PENDING
    assert refusal_reason(service.approve, MAIN_AGENT, PROJECT, second) is Reason.STALE_BASE
    assert refusal_reason(service.approve, MAIN_AGENT, PROJECT, "c_missing") is Reason.NOT_FOUND
    assert refusal_reason(service.reject, MAIN_AGENT, PROJECT, "c_missing") is Reason.NOT_FOUND


def test_a_change_of_another_project_is_not_found() -> None:
    service, _store, _clock = seeded()
    change_id = propose(service, OWNER, put(note(2, "EXG-02")))
    other = Principal("main-agent", MAIN_AGENT.kind)
    assert refusal_reason(service.approve, other, OTHER_PROJECT, change_id) is Reason.NOT_FOUND


def test_a_proposal_that_breaks_a_reference_is_refused() -> None:
    service, _store, _clock = seeded()
    assert (
        refusal_reason(service.propose, OWNER, PROJECT, change(put(note(2, "EXG-77"))))
        is Reason.INVALID_REQUEST
    )
    assert service.pending(OPERATOR, PROJECT) == []


def test_what_the_owner_may_change_without_the_operator() -> None:
    service, _store, _clock = seeded()
    allowed = [
        put(story(1, 1, "A better title")),
        put(note(2, "EXG-01", "More context.")),
        put(criterion(1, 2, 1, "Reworded criterion of a plain story.")),
        put(criterion(1, 2, 2)),
        put(story(1, 3)),
        put(link(1, 3, "EXG-02")),
    ]
    for operation in allowed:
        change_id = propose(service, OWNER, operation)
        service.approve(MAIN_AGENT, PROJECT, change_id)
    final = service.read(OPERATOR, PROJECT)
    assert final.objects["US-E1-01"]["title"] == "A better title"
    assert "LNK:US-E1-03>EXG-02" in final.objects
    assert (
        check_floor([service.read(OPERATOR, PROJECT, n) for n in range(1, final.number + 1)]) == []
    )


# ----------------------------------------------------------------------------- ATK-88
RESERVED_ATTEMPTS = {
    "text of a requirement": [put(requirement(1, text="Another text.", priority="P0"))],
    "priority of a requirement": [put(requirement(2, priority="P0"))],
    "demote a P0 requirement": [put(requirement(1, priority="P1"))],
    "methods of a requirement": [put(requirement(2, methods=("analysis",)))],
    "text of a constraint": [put({**constraint(1, "P0"), "text": "Changed."})],
    "new requirement": [put(requirement(3))],
    "delete a requirement": [delete("LNK:US-E1-02>EXG-02"), delete("EXG-02")],
    "unlink a story from a P0 requirement": [delete("LNK:US-E1-01>EXG-01")],
    "link a story to a P0 requirement": [put(link(1, 2, "EXG-01"))],
    "link a new story to a P0 requirement": [put(story(1, 3)), put(link(1, 3, "EXG-01"))],
    "edit a P0 criterion": [put(criterion(1, 1, 1, "Weakened."))],
    "delete a P0 criterion": [delete("US-E1-01.1")],
    "add a P0 criterion": [put(criterion(1, 1, 2))],
    "unlink then edit in one change": [
        delete("LNK:US-E1-01>EXG-01"),
        put(criterion(1, 1, 1, "Weakened.")),
    ],
    "promote a requirement so that a plain story turns P0": [put(requirement(2, priority="P0"))],
}


@pytest.mark.parametrize("name", RESERVED_ATTEMPTS)
@pytest.mark.parametrize("principal", [OWNER, MAIN_AGENT], ids=["owner", "main-agent"])
def test_the_owner_side_cannot_change_reserved_data(principal: Principal, name: str) -> None:
    service, _store, _clock = seeded()
    before = service.read(OPERATOR, PROJECT)
    assert (
        refusal_reason(service.propose, principal, PROJECT, change(*RESERVED_ATTEMPTS[name]))
        is Reason.RESERVED_DATA
    )
    after = service.read(OPERATOR, PROJECT)
    assert after.id == before.id
    assert reserved_view(after.objects) == reserved_view(before.objects)
    assert service.pending(OPERATOR, PROJECT) == []


def test_the_operator_makes_the_same_changes() -> None:
    for name, operations in RESERVED_ATTEMPTS.items():
        service, _store, _clock = seeded()
        version = service.apply_reserved(OPERATOR, PROJECT, change(*operations))
        assert version.operator_written, name
        assert version.number == 2


def test_unlinking_does_not_turn_a_p0_criterion_into_a_plain_one() -> None:
    service, _store, _clock = seeded()
    # The Owner side cannot remove the link, so the criterion stays a P0 criterion.
    assert (
        refusal_reason(service.propose, OWNER, PROJECT, change(delete("LNK:US-E1-01>EXG-01")))
        is Reason.RESERVED_DATA
    )
    assert (
        refusal_reason(
            service.propose, OWNER, PROJECT, change(put(criterion(1, 1, 1, "Weakened.")))
        )
        is Reason.RESERVED_DATA
    )


def test_a_pending_change_that_became_reserved_is_refused_at_approval() -> None:
    service, _store, _clock = seeded()
    change_id = propose(service, OWNER, put(criterion(1, 2, 1, "Reworded.")))
    # The operator promotes the second requirement: the criterion becomes a P0 criterion.
    service.apply_reserved(OPERATOR, PROJECT, change(put(requirement(2, priority="P0"))))
    assert refusal_reason(service.approve, MAIN_AGENT, PROJECT, change_id) is Reason.STALE_BASE


# ----------------------------------------------------------------------------- reads
def test_reads_follow_the_rights() -> None:
    service, _store, _clock = seeded()
    assert service.read(CONTRACTOR, PROJECT).number == 1
    assert service.read(OWNER, PROJECT, 1).number == 1
    assert service.read(OPERATOR, PROJECT, 0).number == 0
    assert refusal_reason(service.read, FOREIGN_OWNER, PROJECT) is Reason.OTHER_PROJECT
    assert refusal_reason(service.read, CONTRACTOR, OTHER_PROJECT) is Reason.OTHER_PROJECT
    assert refusal_reason(service.read, OPERATOR, PROJECT, 9) is Reason.NOT_FOUND
    assert refusal_reason(service.pending, FOREIGN_OWNER, PROJECT) is Reason.OTHER_PROJECT


def test_who_may_decide_a_change_reads_it_first_and_the_contractor_does_not() -> None:
    """ADR 0020: the second real run's operator could not read what it approved."""
    service, _store, _clock = seeded()
    change_id = propose(service, OWNER, put(criterion(1, 2, 1, "Reworded.")))
    for reader in (OPERATOR, MAIN_AGENT, OWNER):
        [found] = service.pending(reader, PROJECT)
        assert found.id == change_id
        assert [op.op for op in found.operations] == ["put"]
    assert refusal_reason(service.pending, CONTRACTOR, PROJECT) is Reason.FORBIDDEN


def test_a_project_without_a_contract_reads_as_empty() -> None:
    service, _store, _clock = make_service()
    version = service.read(OWNER, PROJECT)
    assert (version.number, dict(version.objects)) == (0, {})


def test_two_versions_with_the_same_content_have_different_identifiers() -> None:
    service, _store, _clock = seeded()
    change_a = propose(service, OWNER, put(note(2, "EXG-02")))
    first = service.approve(MAIN_AGENT, PROJECT, change_a)
    change_b = propose(service, OWNER, delete("NB-002"))
    second = service.approve(MAIN_AGENT, PROJECT, change_b)
    assert second.content_hash == service.read(OPERATOR, PROJECT, 1).content_hash
    assert len({service.read(OPERATOR, PROJECT, n).id for n in (1, 2, 3)}) == 3
    assert first.id != second.id


# ----------------------------------------------------------------------------- the floor holds
def test_check_floor_finds_a_forged_history() -> None:
    service, _store, _clock = seeded()
    change_id = propose(service, OWNER, put(note(2, "EXG-02")))
    service.approve(MAIN_AGENT, PROJECT, change_id)
    versions = [service.read(OPERATOR, PROJECT, n) for n in (1, 2)]
    assert check_floor(versions) == []
    forged_objects = {
        **versions[1].objects,
        "EXG-01": requirement(1, text="Forged.", priority="P0"),
    }
    import dataclasses

    forged = dataclasses.replace(versions[1], objects=forged_objects)
    assert any("reserved data changed" in f for f in check_floor([versions[0], forged]))
    repeated = dataclasses.replace(versions[1], id=versions[0].id)
    assert any("repeats" in f for f in check_floor([versions[0], repeated]))
    orphan = dataclasses.replace(versions[1], parent="v9-xxxxxxxxxxxx")
    assert any("parent" in f for f in check_floor([versions[0], orphan]))


CANDIDATES = [
    put(requirement(1, text="Forged.", priority="P0")),
    put(requirement(2, priority="P0")),
    put(requirement(3)),
    delete("LNK:US-E1-01>EXG-01"),
    put(link(1, 2, "EXG-01")),
    put(criterion(1, 1, 1, "Forged criterion.")),
    put(criterion(1, 2, 1, "Reworded.")),
    put(criterion(1, 1, 3)),
    put(story(1, 1, "Retitled")),
    put(story(1, 4)),
    put(note(4, "EXG-01", "A note.")),
    delete("NB-001"),
    put(link(1, 2, "CTR-01")),
    delete("US-E1-02.1"),
    delete("CTR-01"),
]


@settings(max_examples=60, deadline=None)
@given(
    steps=st.lists(
        st.tuples(
            st.sampled_from(range(len(ALL_PRINCIPALS))),
            st.sampled_from(range(len(ALL_PRINCIPALS))),
            st.lists(st.sampled_from(range(len(CANDIDATES))), min_size=1, max_size=3),
            st.booleans(),
        ),
        min_size=1,
        max_size=14,
    )
)
def test_whatever_the_callers_do_the_reserved_data_follows_the_operator_alone(steps) -> None:  # type: ignore[no-untyped-def]  # reason: hypothesis supplies the argument
    service, _store, _clock = seeded()
    baseline = reserved_view(service.read(OPERATOR, PROJECT).objects)
    for proposer_index, approver_index, picks, operator_applies in steps:
        edit = change(*(CANDIDATES[i] for i in picks))
        proposer, approver = ALL_PRINCIPALS[proposer_index], ALL_PRINCIPALS[approver_index]
        try:
            if operator_applies:
                version = service.apply_reserved(proposer, PROJECT, edit)
                baseline = reserved_view(version.objects)
            else:
                pending = service.propose(proposer, PROJECT, edit)
                service.approve(approver, PROJECT, pending.id)
        except Refusal:
            pass
        current = service.read(OPERATOR, PROJECT)
        history = [service.read(OPERATOR, PROJECT, n) for n in range(1, current.number + 1)]
        assert check_floor(history) == []
        assert reserved_view(current.objects) == baseline
        assert len({v.id for v in history}) == len(history)
