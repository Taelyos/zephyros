"""The Markdown importer: the document of a project becomes contract objects (Q-09)."""

from __future__ import annotations

import pytest

from tests.unit.core.hub_fixtures import Env, make_env, seed_contract
from zephyros.core.contract import ContractError
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


# ---------------------------------------------------------------------- a document gone wrong

HEADER = (
    "| ID | Requirement | Verification | Priority | Rationale |\n| --- | --- | --- | --- | --- |\n"
)


def row(ident: str, verification: str = "T", priority: str = "P1", text: str = "say.") -> str:
    return f"| {ident} | The system shall {text} | {verification} | {priority} | Why |\n"


def refusal_of(document: str) -> str:
    with pytest.raises(ContractError) as caught:
        import_markdown(document)
    return str(caught.value)


def test_a_requirement_defined_twice_is_refused_and_named_with_its_lines() -> None:
    """The second row used to replace the first in silence."""
    document = HEADER + row("EXG-01", text="say a.") + row("EXG-02") + row("EXG-01", text="say b.")
    assert refusal_of(document) == "EXG-01: defined more than once, lines 3 and 5"


def test_a_story_defined_twice_is_refused_and_its_criteria_are_not_mixed() -> None:
    story = (
        "#### US-E01-01 · Epic E01\n\n"
        "As a **user**, I want **to {want}**, so that **it works**.\n\n"
        "- The system shall {want}. (T)\n"
    )
    document = story.format(want="read") + "\n" + story.format(want="write")
    assert refusal_of(document) == "US-E01-01: defined more than once, lines 1 and 7"


def test_every_duplicate_is_named_at_once() -> None:
    document = HEADER + row("EXG-01") + row("CTR-01") + row("EXG-01") + row("CTR-01")
    assert refusal_of(document) == (
        "EXG-01: defined more than once, lines 3 and 5; "
        "CTR-01: defined more than once, lines 4 and 6"
    )


def test_the_same_link_stated_twice_is_one_link() -> None:
    """A link has no content of its own: stating it twice loses nothing, so it is not refused."""
    document = (
        "#### US-E01-01 · Epic E01\n\n"
        "As a **user**, I want **to read**, so that **it works**.\n\n"
        "Covers: EXG-01, EXG-01\n"
        "Covers: EXG-01\n"
    )
    links = [item for item in import_markdown(document) if item["type"] == "link"]
    assert [link["id"] for link in links] == ["LNK:US-E01-01>EXG-01"]


@pytest.mark.parametrize(
    ("verification", "methods"),
    [
        ("T", ["test"]),
        ("D", ["demonstration"]),
        ("T + A", ["test", "analysis"]),
        ("T+A", ["test", "analysis"]),
        ("I, D", ["inspection", "demonstration"]),
        ("A/T", ["analysis", "test"]),
        ("T (attack catalog) + A", ["test", "analysis"]),
        ("T (ATK-01, A)", ["test"]),
        ("T + T", ["test"]),
    ],
)
def test_the_methods_are_read_as_tokens(verification: str, methods: list[str]) -> None:
    (requirement,) = import_markdown(HEADER + row("EXG-01", verification))
    assert requirement["methods"] == methods


@pytest.mark.parametrize(
    ("verification", "named"),
    [
        ("TBD", "'TBD'"),
        ("T + TBD", "'TBD'"),
        ("Test", "'Test'"),
        ("t", "'t'"),
        ("T and A", "'and'"),
    ],
)
def test_a_method_that_is_not_t_i_a_or_d_is_refused_and_named(
    verification: str, named: str
) -> None:
    """Read letter by letter, "TBD" was a test and a demonstration; read as tokens, it is no
    method at all, and the refusal says which requirement and which word."""
    refusal = refusal_of(HEADER + row("EXG-04", verification))
    assert refusal.startswith(
        f"EXG-04.methods: {named} is not a verification method (T, I, A or D)"
    )


@pytest.mark.parametrize(
    ("line", "cells"),
    [
        ("| EXG-03 | The system shall say. | T | P1 |", 4),
        ("| EXG-03 | The system shall say. | T |", 3),
        ("| CTR-03 | The system shall say. | T | P1 | Why | Extra |", 6),
        ("| EXG-03 |", 1),
    ],
)
def test_a_row_with_the_wrong_number_of_cells_is_refused_not_dropped(line: str, cells: int) -> None:
    """A row the old pattern did not recognise was dropped in silence, and the requirement with
    it."""
    ident = line.split("|")[1].strip()
    assert refusal_of(HEADER + row("EXG-01") + line + "\n") == (
        f"line 4: {ident} has {cells} cells; a requirement or constraint row has five: "
        "ID, text, verification, priority, rationale"
    )


@pytest.mark.parametrize(
    ("line", "named"),
    [
        (row("EXG-03", priority="high"), "EXG-03.priority: must be one of"),
        (row("EXG-03", priority=""), "EXG-03.priority: must be one of"),
        ("| EXG-03 |  | T | P1 | Why |\n", "EXG-03.text: "),
        ("| EXG-03 | The system shall say. |  | P1 | Why |\n", "EXG-03.methods: "),
        (row("EXG-1234"), "EXG-1234.id: "),
        (row("EXG-03x"), "EXG-03x.id: "),
    ],
)
def test_a_row_with_a_bad_cell_is_refused_and_named(line: str, named: str) -> None:
    """Every row whose first cell is a requirement or constraint identifier is read, and the
    refusal names the object and the field at fault."""
    assert named in refusal_of(HEADER + line)


def test_a_story_without_its_sentence_is_refused_and_named() -> None:
    refusal = refusal_of("#### US-E01-01 · Epic E01\n\n- The system shall say. (T)\n")
    assert "US-E01-01.title: " in refusal
    assert "US-E01-01.text: " in refusal


def test_a_row_without_its_closing_pipe_or_with_an_escaped_pipe_is_read() -> None:
    document = HEADER + "| EXG-01 | The system shall keep a \\| b. | T | P0 | Why\n"
    assert import_markdown(document) == [
        {
            "type": "requirement",
            "id": "EXG-01",
            "text": "The system shall keep a | b.",
            "priority": "P0",
            "methods": ["test"],
        }
    ]


def test_a_table_that_is_not_a_requirement_table_stays_prose() -> None:
    document = (
        "| Story | Covers |\n| --- | --- |\n| US-E01-01 | EXG-01 |\n\n"
        "| ID | Note |\n| --- | --- |\n| NB-001 | x |\n"
    )
    assert import_markdown(document) == []


def test_the_operator_is_told_which_identifier_is_defined_twice() -> None:
    env: Env = make_env()
    seed_contract(env)
    document = HEADER + row("EXG-07", text="keep it.") + row("EXG-07", text="drop it.")
    response = env.call(
        "operator", "contract.import", project_id=env.project_a, summary="twice", markdown=document
    )
    assert not response.ok
    assert response.failure is not None
    assert response.failure.reason.value == "invalid_request"
    assert "EXG-07: defined more than once, lines 3 and 4" in response.failure.message
    reading = env.call("operator", "contract.read", project_id=env.project_a)
    assert reading.data is not None
    objects = reading.data["objects"]
    assert isinstance(objects, dict)
    assert "EXG-07" not in objects


def test_the_operator_is_told_which_method_is_not_one() -> None:
    env: Env = make_env()
    seed_contract(env)
    response = env.call(
        "operator",
        "contract.import",
        project_id=env.project_a,
        summary="to be decided",
        markdown=HEADER + row("EXG-08", "TBD"),
    )
    assert not response.ok
    assert response.failure is not None
    assert "EXG-08.methods: 'TBD' is not a verification method" in response.failure.message
