"""The Markdown importer: the document of a project becomes contract objects (Q-09)."""

from __future__ import annotations

from tests.unit.core.hub_fixtures import Env, make_env, seed_contract
from zephyros.core.importer import import_markdown

DOCUMENT = """\
# The document of the project

## Requirements

| ID | Requirement | Verification | Priority | Rationale |
| --- | --- | --- | --- | --- |
| EXG-01 | The system shall keep the state of every task in the hub. | T | P0 | One place to look |
| EXG-02 | The system shall refuse a failed delivery. | T (attack catalog) + A | P1 | Not silent |
| CTR-01 | The system shall run on a machine of 8 GB of RAM. | I | P1 | A homelab machine |

## Stories

### Stories of epic E01

#### US-E01-01 · Epic E01

As a **user**, I want **to see the state of my project**, so that **I know what is running**.

Covers: EXG-01 (the first look; the rest comes later)

- When the user opens the project, the system shall show the state of each task. (T)
- If the state is unknown, then the system shall say so. (ATK-01)
- The system shall read the state from the hub. (I)

Prose between stories, with a - dash that is not a bullet at the start of a line.

## Notes

- A bullet outside any story stays prose.
"""


def test_the_tables_become_requirements_and_constraints() -> None:
    objects = import_markdown(DOCUMENT)
    assert objects[0] == {
        "type": "requirement",
        "id": "EXG-01",
        "text": "The system shall keep the state of every task in the hub.",
        "priority": "P0",
        "methods": ["test"],
    }
    assert objects[2] == {
        "type": "constraint",
        "id": "CTR-01",
        "text": "The system shall run on a machine of 8 GB of RAM.",
        "priority": "P1",
        "methods": ["inspection"],
    }


def test_a_verification_with_parenthesis_and_plus_gives_its_methods() -> None:
    exg_02 = next(item for item in import_markdown(DOCUMENT) if item["id"] == "EXG-02")
    assert exg_02["methods"] == ["test", "analysis"]


def test_a_story_section_gives_the_story_its_criteria_and_its_links() -> None:
    objects = {item["id"]: item for item in import_markdown(DOCUMENT)}
    assert objects["US-E01-01"] == {
        "type": "story",
        "id": "US-E01-01",
        "epic": "E01",
        "title": "to see the state of my project",
        "text": "As a user, I want to see the state of my project, so that I know what is running.",
    }
    assert objects["LNK:US-E01-01>EXG-01"] == {
        "type": "link",
        "id": "LNK:US-E01-01>EXG-01",
        "kind": "covers",
        "source": "US-E01-01",
        "target": "EXG-01",
    }
    assert objects["US-E01-01.1"] == {
        "type": "criterion",
        "id": "US-E01-01.1",
        "story": "US-E01-01",
        "text": "When the user opens the project, the system shall show the state of each task.",
        "verification": "T",
    }
    assert objects["US-E01-01.2"]["verification"] == "ATK-01"
    assert objects["US-E01-01.3"] == {
        "type": "criterion",
        "id": "US-E01-01.3",
        "story": "US-E01-01",
        "text": "The system shall read the state from the hub.",
        "verification": "I",
    }


def test_prose_bullets_and_headings_close_a_story_section() -> None:
    ids = [str(item["id"]) for item in import_markdown(DOCUMENT)]
    assert "US-E01-01.4" not in ids
    assert len([one for one in ids if one.startswith("US-E01-01.")]) == 3


def test_a_document_without_a_single_object_imports_nothing() -> None:
    assert import_markdown("# Prose\n\nNothing here.\n") == []


def test_the_operator_imports_a_document_into_the_contract() -> None:
    env: Env = make_env()
    seed_contract(env)
    response = env.call(
        "operator",
        "contract.import",
        project_id=env.project_a,
        summary="the document",
        markdown=DOCUMENT,
    )
    assert response.ok, response.failure
    assert response.data is not None
    assert response.data["objects"] == 8
    assert response.data["kinds"] == {
        "constraint": 1,
        "criterion": 3,
        "link": 1,
        "requirement": 2,
        "story": 1,
    }
    assert env.last(env.project_a).action.value == "contract.import"
    reading = env.call("operator", "contract.read", project_id=env.project_a)
    assert reading.data is not None
    objects = reading.data["objects"]
    assert isinstance(objects, dict)
    assert "EXG-01" in objects
    assert "US-E01-01.2" in objects


def test_only_the_operator_imports_a_document() -> None:
    env: Env = make_env()
    seed_contract(env)
    response = env.call(
        "contractor_a",
        "contract.import",
        project_id=env.project_a,
        summary="the document",
        markdown=DOCUMENT,
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason.value == "forbidden"


def test_a_document_with_a_broken_object_is_refused_with_its_problems() -> None:
    env: Env = make_env()
    seed_contract(env)
    broken = (
        "| ID | Requirement | Verification | Priority | Rationale |\n"
        "| EXG-9 | The system shall say. | T | X9 | Why |\n"
    )
    response = env.call(
        "operator", "contract.import", project_id=env.project_a, summary="broken", markdown=broken
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason.value == "invalid_request"
