"""ATK-71: the refusals of one instance are capped, and the window slides."""

from __future__ import annotations

from zephyros.core.limits import RefusalBudget


def test_the_cap_is_reached_within_the_minute() -> None:
    budget = RefusalBudget(3)
    assert not budget.over("contractor:p_1", 100.0)
    for moment in (100.0, 101.0, 102.0):
        budget.refused("contractor:p_1", moment)
    assert budget.over("contractor:p_1", 103.0)


def test_the_window_slides_so_a_calm_instance_is_served_again() -> None:
    budget = RefusalBudget(2)
    budget.refused("contractor:p_1", 0.0)
    budget.refused("contractor:p_1", 1.0)
    assert budget.over("contractor:p_1", 2.0)
    assert not budget.over("contractor:p_1", 62.0)  # both refusals left the window


def test_each_instance_has_its_own_budget() -> None:
    budget = RefusalBudget(1)
    budget.refused("contractor:p_1", 0.0)
    assert budget.over("contractor:p_1", 1.0)
    assert not budget.over("contractor:p_2", 1.0)
    assert not budget.over("owner:p_1", 1.0)
