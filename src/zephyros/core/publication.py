"""The schemas the project publishes in `schemas/`, each with a version number.

A CI step regenerates them and fails when a committed file differs (EXG-50, CTR-13).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

from zephyros.core.config import SCHEMA as CONFIGURATION_SCHEMA
from zephyros.core.contract import contract_json_schema
from zephyros.core.hub import TOOL_SPECS
from zephyros.core.principals import PrincipalKind
from zephyros.core.rights import PERMISSIONS


def render(document: Mapping[str, object]) -> str:
    """The canonical text of a schema document: two-space indentation, one final newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def published_schemas() -> Mapping[str, str]:
    """File name to canonical text, for every published schema."""
    return MappingProxyType(
        {
            "configuration.schema.json": render(CONFIGURATION_SCHEMA.json_schema()),
            "contract.schema.json": render(contract_json_schema()),
        }
    )


def published_documents() -> Mapping[str, str]:
    """Path (from the repository root) to text, for every generated document."""
    return MappingProxyType({"docs/tools.md": render_tools_doc()})


_KINDS = (
    (PrincipalKind.OPERATOR, "Operator"),
    (PrincipalKind.MAIN_AGENT, "Main agent"),
    (PrincipalKind.OWNER, "Owner instance"),
    (PrincipalKind.CONTRACTOR, "Contractor instance"),
)
_SCOPE = {"none": "no", "own": "own project", "any": "yes"}


def render_tools_doc() -> str:
    """The text of docs/tools.md: the tools of the hub and who may use each (US-E1-07)."""
    header = "| Tool | What it does | " + " | ".join(name for _, name in _KINDS) + " |"
    divider = "| --- | --- | " + " | ".join("---" for _ in _KINDS) + " |"
    rows = []
    for spec in TOOL_SPECS.values():
        scopes = " | ".join(_SCOPE[PERMISSIONS[spec.action][kind].value] for kind, _ in _KINDS)
        rows.append(f"| `{spec.name}` | {spec.description} | {scopes} |")
    contractor = [
        f"`{spec.name}`"
        for spec in TOOL_SPECS.values()
        if PERMISSIONS[spec.action][PrincipalKind.CONTRACTOR].value != "none"
    ]
    return "\n".join(
        [
            "# Tools of the hub",
            "",
            "<!-- Generated from the registry of tools and the rights matrix: do not edit by hand. "
            "Regenerate with `python -m tools.generate_schemas`. -->",
            "",
            "An agent reaches the hub through MCP on its agent endpoint, and the operator "
            "reaches it through the operator channel. Both use the same registry. A tool that "
            "a caller may not use is not listed to that caller, and calling it by name is "
            "refused and logged. Every call, allowed or refused, leaves exactly one record "
            "in the log.",
            "",
            '"Own project" means the project of the caller\'s token. An instance that names '
            "another project in a call is refused. Unknown fields in a call are refused.",
            "",
            header,
            divider,
            *rows,
            "",
            "## What a Contractor instance reaches on the hub",
            "",
            ", ".join(contractor)
            + ". It reads the contract as data and cannot write it, propose a "
            "change to it, approve one, write an acceptance test, or reach the log, the "
            "configuration or the instances. It declares a delivery and reads its state; it "
            "never records a result.",
            "",
            "Besides these tools, a Contractor instance calls with its project token the model "
            "gateway (`/v1`) and the staging area of its project: `GET /staging/base` for the "
            "base to start from and `POST /staging/delivery?branch=<name>` to push its work, both "
            "as git bundles. The tools of the agent engine itself, inside the container, are "
            "documented with the engine integration.",
            "",
        ]
    )
