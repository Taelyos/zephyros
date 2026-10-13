"""Shared builders for the contract tests."""

from __future__ import annotations

from typing import Any

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.contract_store import InMemoryContractStore
from zephyros.core.contract import Change, ContractService, Operation, parse_change
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import MAIN_AGENT, OPERATOR, Principal, instance_principal

PROJECT = "p_abcdef12"
OTHER_PROJECT = "p_zzzzzz99"
OWNER = instance_principal(Side.OWNER, PROJECT)
OWNER_TWO = instance_principal(Side.OWNER, PROJECT, "two")
CONTRACTOR = instance_principal(Side.CONTRACTOR, PROJECT)
FOREIGN_OWNER = instance_principal(Side.OWNER, OTHER_PROJECT)
ALL_PRINCIPALS = (OPERATOR, MAIN_AGENT, OWNER, OWNER_TWO, CONTRACTOR, FOREIGN_OWNER)


def requirement(
    number: int,
    text: str = "The system shall do it.",
    priority: str = "P1",
    methods: tuple[str, ...] = ("test",),
) -> dict[str, Any]:
    return {
        "type": "requirement",
        "id": f"EXG-{number:02d}",
        "text": text,
        "priority": priority,
        "methods": list(methods),
    }


def constraint(number: int, priority: str = "P1") -> dict[str, Any]:
    return {
        "type": "constraint",
        "id": f"CTR-{number:02d}",
        "text": "The constraint.",
        "priority": priority,
        "methods": ["inspection"],
    }


def story(epic: int, number: int, title: str = "A story") -> dict[str, Any]:
    return {
        "type": "story",
        "id": f"US-E{epic}-{number:02d}",
        "epic": f"E{epic}",
        "title": title,
        "text": "As a user, I want it, so that it works.",
    }


def criterion(
    epic: int, number: int, index: int, text: str = "The system shall comply."
) -> dict[str, Any]:
    story_id = f"US-E{epic}-{number:02d}"
    return {
        "type": "criterion",
        "id": f"{story_id}.{index}",
        "story": story_id,
        "text": text,
        "verification": "T",
    }


def link(epic: int, number: int, target: str) -> dict[str, Any]:
    source = f"US-E{epic}-{number:02d}"
    return {
        "type": "link",
        "id": f"LNK:{source}>{target}",
        "kind": "covers",
        "source": source,
        "target": target,
    }


def note(number: int, target: str, text: str = "A note.") -> dict[str, Any]:
    return {"type": "note", "id": f"NB-{number:03d}", "target": target, "text": text}


def put(obj: dict[str, Any]) -> Operation:
    return Operation("put", object=obj)


def delete(identifier: str) -> Operation:
    return Operation("delete", id=identifier)


def change(*operations: Operation, summary: str = "a change") -> Change:
    return Change(summary, tuple(operations))


def seed_operations() -> list[Operation]:
    """EXG-01 (P0) and EXG-02 (P1); two stories, each covering one; a P0 criterion and a plain
    one; a note."""
    return [
        put(requirement(1, priority="P0")),
        put(requirement(2, priority="P1")),
        put(constraint(1, priority="P0")),
        put(story(1, 1)),
        put(story(1, 2)),
        put(link(1, 1, "EXG-01")),
        put(link(1, 2, "EXG-02")),
        put(criterion(1, 1, 1, "The system shall refuse the write.")),
        put(criterion(1, 2, 1)),
        put(note(1, "EXG-01")),
    ]


def make_service() -> tuple[ContractService, InMemoryContractStore, FakeClock]:
    store = InMemoryContractStore()
    clock = FakeClock()
    counter = iter(range(1, 10_000))
    service = ContractService(store, clock, new_change_id=lambda: f"c_{next(counter):08d}")
    return service, store, clock


def seeded(
    principal: Principal = OPERATOR,
) -> tuple[ContractService, InMemoryContractStore, FakeClock]:
    service, store, clock = make_service()
    service.apply_reserved(principal, PROJECT, change(*seed_operations(), summary="import"))
    return service, store, clock


def wire(operation: Operation) -> dict[str, Any]:
    return (
        {"op": "put", "object": operation.object}
        if operation.op == "put"
        else {"op": "delete", "id": operation.id}
    )


def parse(*operations: Operation, summary: str = "a change") -> Change:
    return parse_change({"summary": summary, "operations": [wire(o) for o in operations]})
