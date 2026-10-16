"""The generated documents and schemas."""

from __future__ import annotations

import json

from zephyros.core.hub import TOOL_SPECS
from zephyros.core.publication import (
    published_documents,
    published_schemas,
    render,
    render_tools_doc,
)


def test_the_tools_document_lists_every_tool_with_its_rights() -> None:
    text = render_tools_doc()
    for spec in TOOL_SPECS.values():
        assert f"| `{spec.name}` |" in text
    row = next(
        line for line in text.splitlines() if line.startswith("| `contract.apply_reserved` |")
    )
    assert row.endswith("| yes | no | no | no |")
    assert "| `contract.read` |" in text
    assert (
        "`whoami`, `project.list`, `project.get`, `contract.read`, `delivery.declare`, "
        "`delivery.status`, `obstacle.report`, `obstacle.list`, `obstacle.read`, "
        "`obstacle.close`." in text
    )


def test_the_published_files_are_the_canonical_text() -> None:
    schemas = published_schemas()
    assert set(schemas) == {"configuration.schema.json", "contract.schema.json"}
    for text in schemas.values():
        assert text.endswith("}\n")
        assert render(json.loads(text)) == text
    assert set(published_documents()) == {"docs/tools.md"}


def test_the_contract_schema_has_one_branch_per_kind_of_object() -> None:
    document = json.loads(published_schemas()["contract.schema.json"])
    assert [branch["title"] for branch in document["oneOf"]] == [
        "requirement",
        "constraint",
        "story",
        "criterion",
        "link",
        "note",
    ]
    assert document["x-zephyros-schema-version"] == "1.1.0"
    assert all(branch["additionalProperties"] is False for branch in document["oneOf"])
