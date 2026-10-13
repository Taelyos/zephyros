"""Subnet allocation and the instance records, in memory and in SQLite."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zephyros.adapters.memory.instance_store import InMemoryInstanceStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.instance_store import SqliteInstanceStore
from zephyros.core.instances import (
    InstanceRecord,
    InstanceStore,
    NoFreeSubnetError,
    allocate_subnet,
)
from zephyros.core.ports.agent_engine import Side

POOL = "10.200.0.0/16"


# ----------------------------------------------------------------------------- allocation
def test_the_first_subnet_of_the_pool_is_allocated_first() -> None:
    assert allocate_subnet(POOL, 28, []) == "10.200.0.0/28"


def test_taken_subnets_are_skipped_in_order() -> None:
    assert allocate_subnet(POOL, 28, ["10.200.0.0/28"]) == "10.200.0.16/28"
    assert allocate_subnet(POOL, 28, ["10.200.0.0/28", "10.200.0.32/28"]) == "10.200.0.16/28"


def test_a_hole_left_by_a_stopped_instance_is_reused() -> None:
    taken = [f"10.200.0.{n}/28" for n in (0, 16, 48)]
    assert allocate_subnet(POOL, 28, taken) == "10.200.0.32/28"


def test_a_taken_subnet_of_another_size_still_blocks_what_it_overlaps() -> None:
    assert allocate_subnet(POOL, 28, ["10.200.0.0/24"]) == "10.200.1.0/28"
    assert allocate_subnet("10.200.0.0/24", 28, ["10.200.0.8/29"]) == "10.200.0.16/28"


def test_a_full_pool_is_reported() -> None:
    pool = "10.200.0.0/30"
    with pytest.raises(NoFreeSubnetError):
        allocate_subnet(pool, 30, [pool])
    with pytest.raises(NoFreeSubnetError):
        allocate_subnet("10.200.0.0/27", 28, ["10.200.0.0/28", "10.200.0.16/28"])


def test_a_malformed_pool_or_subnet_is_an_error_not_an_allocation() -> None:
    with pytest.raises(ValueError, match="does not appear"):
        allocate_subnet("nonsense", 28, [])
    with pytest.raises(ValueError, match="host bits"):
        allocate_subnet(POOL, 28, ["10.200.0.1/28"])


@given(
    taken=st.sets(st.integers(min_value=0, max_value=63), max_size=40),
)
def test_an_allocation_is_inside_the_pool_free_and_the_first_such(taken: set[int]) -> None:
    pool = ipaddress.IPv4Network("10.200.0.0/22")
    used = [str(ipaddress.IPv4Network(f"10.200.{n // 16}.{(n % 16) * 16}/28")) for n in taken]
    try:
        chosen = ipaddress.IPv4Network(allocate_subnet(str(pool), 28, used))
    except NoFreeSubnetError:
        assert len(taken) == 64
        return
    assert chosen.subnet_of(pool)
    assert not any(chosen.overlaps(ipaddress.IPv4Network(u)) for u in used)
    free_before = [
        s
        for s in pool.subnets(new_prefix=28)
        if s < chosen and not any(s.overlaps(ipaddress.IPv4Network(u)) for u in used)
    ]
    assert free_before == []
    assert allocate_subnet(str(pool), 28, used) == str(chosen)  # deterministic


# ----------------------------------------------------------------------------- stores
def record(project: str, side: Side, number: int) -> InstanceRecord:
    return InstanceRecord(
        project,
        side,
        f"i_{number:012d}",
        f"10.200.0.{number * 16}/28",
        datetime(2026, 10, 10, 12, 0, number, tzinfo=UTC),
    )


def memory() -> InstanceStore:
    return InMemoryInstanceStore()


def sqlite() -> InstanceStore:
    return SqliteInstanceStore(Database(":memory:"))


@pytest.fixture(params=[memory, sqlite], ids=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> InstanceStore:
    make: Callable[[], InstanceStore] = request.param
    return make()


def test_a_record_is_stored_and_read_back_whole(store: InstanceStore) -> None:
    made = record("p_abcdef12", Side.OWNER, 1)
    assert store.get("p_abcdef12", Side.OWNER) is None
    store.put(made)
    assert store.get("p_abcdef12", Side.OWNER) == made
    assert store.get("p_abcdef12", Side.CONTRACTOR) is None
    assert store.get("p_zzzzzz99", Side.OWNER) is None


def test_the_records_are_listed_in_a_stable_order(store: InstanceStore) -> None:
    store.put(record("p_zzzzzz99", Side.OWNER, 3))
    store.put(record("p_abcdef12", Side.CONTRACTOR, 2))
    store.put(record("p_abcdef12", Side.OWNER, 1))
    assert [(r.project_id, r.side.value) for r in store.all()] == [
        ("p_abcdef12", "contractor"),
        ("p_abcdef12", "owner"),
        ("p_zzzzzz99", "owner"),
    ]


def test_a_record_is_removed_and_removing_twice_is_harmless(store: InstanceStore) -> None:
    store.put(record("p_abcdef12", Side.OWNER, 1))
    store.remove("p_abcdef12", Side.OWNER)
    store.remove("p_abcdef12", Side.OWNER)
    assert store.all() == []


def test_putting_again_replaces_the_record(store: InstanceStore) -> None:
    store.put(record("p_abcdef12", Side.OWNER, 1))
    newer = record("p_abcdef12", Side.OWNER, 2)
    store.put(newer)
    assert store.all() == [newer]


def test_sqlite_refuses_two_instances_with_one_subnet() -> None:
    store = SqliteInstanceStore(Database(":memory:"))
    store.put(record("p_abcdef12", Side.OWNER, 1))
    clash = InstanceRecord(
        "p_abcdef12",
        Side.CONTRACTOR,
        "i_other",
        "10.200.0.16/28",
        datetime(2026, 10, 10, tzinfo=UTC),
    )
    with pytest.raises(Exception, match="UNIQUE"):
        store.put(clash)


def test_sqlite_keeps_the_other_record_when_a_clashing_one_is_refused() -> None:
    store = SqliteInstanceStore(Database(":memory:"))
    kept = record("p_abcdef12", Side.OWNER, 1)
    store.put(kept)
    clash = InstanceRecord(
        "p_abcdef12", Side.CONTRACTOR, "i_other", kept.subnet, datetime(2026, 10, 10, tzinfo=UTC)
    )
    with pytest.raises(Exception, match="UNIQUE"):
        store.put(clash)
    assert store.all() == [kept]
