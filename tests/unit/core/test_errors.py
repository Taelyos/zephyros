"""The error items (EXG-51): a stable code, a cause, a documented corrective action."""

from __future__ import annotations

from pathlib import Path

from tests.unit.core.contract_fixtures import put, requirement, wire
from tests.unit.core.hub_fixtures import Env, make_env, seed_contract
from zephyros.core.audit import Reason
from zephyros.core.errors import REMEDIES, remedy

DOC = next(
    parent / "docs" / "errors.md"
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").exists()
)


def test_every_refusal_code_has_a_remedy() -> None:
    """A new code in `Reason` without an action fails here, before any user meets it."""
    assert set(Reason) == set(REMEDIES)


def test_every_remedy_says_something() -> None:
    assert all(text.strip().endswith(".") for text in REMEDIES.values())


def test_the_document_lists_every_code_with_its_exact_action() -> None:
    text = DOC.read_text(encoding="utf-8")
    for reason in Reason:
        assert f"| `{reason.value}` | {REMEDIES[reason]} |" in text


def test_a_refused_call_carries_its_documented_action() -> None:
    env: Env = make_env()
    seed_contract(env)
    response = env.call(
        "contractor_a",
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="by the wrong side",
        operations=[wire(put(requirement(7)))],
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason is Reason.FORBIDDEN
    assert response.failure.action == remedy(Reason.FORBIDDEN)
