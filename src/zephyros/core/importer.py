"""Importing a contract from the Markdown document of a project.

The document is written the way the method asks:
requirement and constraint tables, one row per object; story sections, one heading per story
(`#### US-Enn-nn · Epic Enn`), the story's sentence, a `Covers:` line, and the acceptance
criteria as bullets. The importer reads those four shapes and nothing else: the rest of the
document is prose and stays prose.

A document is imported whole or not at all. Every fault is named, with the object and the field
or the line: an identifier defined twice, a row of the wrong width, a verification method that is
not T, I, A or D, and whatever the contract schema refuses in an object.
"""

from __future__ import annotations

import re

from zephyros.core.contract import ContractError, validate_object
from zephyros.core.schema import Problem

_METHODS = {"T": "test", "I": "inspection", "A": "analysis", "D": "demonstration"}

_ROW_ID = re.compile(r"^(?:EXG|CTR)-")
_CELL_EDGE = re.compile(r"(?<!\\)\|")
_ROW_CELLS = 5
_STORY = re.compile(
    r"^#{2,6}\s+(?P<id>US-E[0-9]{1,2}-[0-9]{1,2})\s*·\s*Epic\s+(?P<epic>E[0-9]{1,2})\s*$"
)
_AS_A = re.compile(
    r"^As a \*\*(?P<user>[^*]+)\*\*,\s*I want \*\*(?P<want>[^*]+)\*\*,\s*"
    r"so that \*\*(?P<why>[^*]+)\*\*\.\s*$"
)
_COVERS = re.compile(r"^Covers:\s*(?P<ids>.+)$")
_BULLET = re.compile(r"^-\s+(?P<text>.+)$")
_MARKER = re.compile(r"^(?P<text>.*?)\s*\((?P<mark>[^()]*)\)\s*$")
_PARENTHETICAL = re.compile(r"\([^()]*\)")
_METHOD_TOKEN = re.compile(r"[^\s+,/;&]+")
_IDS = re.compile(r"\b((?:EXG|CTR)-[0-9]{1,3})\b")


def _cells(line: str) -> list[str]:
    """The cells of a table row: split on the pipes that are not escaped, the closing pipe
    optional, an escaped pipe read as a pipe."""
    inner = _CELL_EDGE.split(line)[1:]
    if inner and not inner[-1].strip():
        inner.pop()
    return [cell.strip().replace("\\|", "|") for cell in inner]


def _is_requirement_header(cells: list[str]) -> bool:
    """A requirement or constraint table is told by its header (`| ID | Requirement |
    Verification | Priority | Rationale |`), not by its rows: a coverage table (`| ID | Stories |
    Remark |`) also starts its rows with requirement identifiers, and stays prose."""
    names = [cell.lower() for cell in cells]
    return bool(names) and names[0] == "id" and "verification" in names and "priority" in names


def _methods(ident: str, verification: str, problems: list[Problem]) -> list[str]:
    """The methods of a verification cell, read as tokens separated by spaces or `+ , / ; &`,
    a parenthesis being a comment: "T (attack catalog) + A" is a test and an analysis, and
    "TBD" is no method at all, not a test and a demonstration."""
    found: list[str] = []
    for token in _METHOD_TOKEN.findall(_PARENTHETICAL.sub(" ", verification)):
        method = _METHODS.get(token)
        if method is None:
            problems.append(
                Problem(
                    f"{ident}.methods",
                    f"{token!r} is not a verification method (T, I, A or D)",
                )
            )
        elif method not in found:
            found.append(method)
    return found


def import_markdown(text: str) -> list[dict[str, object]]:
    """The contract objects the document holds, in the order of the document: requirements and
    constraints from the tables, then each story with its criteria and its links.

    Raises `ContractError` naming every fault when the document cannot be imported as written:
    an identifier defined twice (nothing is replaced in silence), a row of a requirement or
    constraint table that is not five cells wide (nothing is dropped in silence), a method that
    is not T, I, A or D, and every problem the contract schema finds in an object, under the
    object's identifier."""
    objects: dict[str, dict[str, object]] = {}
    lines_of: dict[str, list[int]] = {}
    problems: list[Problem] = []
    requirements: bool | None = None  # in a table: whether it is a requirement table
    story: dict[str, object] | None = None
    criteria = 0

    def define(ident: str, item: dict[str, object], number: int) -> bool:
        lines_of.setdefault(ident, []).append(number)
        if ident in objects:
            return False
        objects[ident] = item
        return True

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = _cells(stripped)
            if requirements is None:
                requirements = _is_requirement_header(cells)
                continue
            if requirements and cells and _ROW_ID.match(cells[0]):
                ident = cells[0]
                if len(cells) != _ROW_CELLS:
                    problems.append(
                        Problem(
                            f"line {number}",
                            f"{ident} has {len(cells)} cells; a requirement or constraint row "
                            "has five: ID, text, verification, priority, rationale",
                        )
                    )
                    continue
                define(
                    ident,
                    {
                        "type": "requirement" if ident.startswith("EXG") else "constraint",
                        "id": ident,
                        "text": cells[1],
                        "priority": cells[3],
                        "methods": _methods(ident, cells[2], problems),
                    },
                    number,
                )
            continue
        requirements = None
        heading = _STORY.match(stripped)
        if heading:
            story = {"type": "story", "id": heading["id"], "epic": heading["epic"]}
            define(heading["id"], story, number)
            criteria = 0
            continue
        if stripped.startswith("#"):
            story = None
            continue
        if story is None:
            continue
        sentence = _AS_A.match(stripped)
        if sentence:
            story["title"] = sentence["want"]
            story["text"] = (
                f"As a {sentence['user']}, I want {sentence['want']}, so that {sentence['why']}."
            )
            continue
        covers = _COVERS.match(stripped)
        if covers:
            for target in _IDS.findall(covers["ids"]):
                link = {
                    "type": "link",
                    "id": f"LNK:{story['id']}>{target}",
                    "kind": "covers",
                    "source": story["id"],
                    "target": target,
                }
                # A link has no content of its own: stated twice, it is one link.
                objects.setdefault(link["id"], link)
            continue
        bullet = _BULLET.match(stripped)
        if bullet:
            criteria += 1
            ident = f"{story['id']}.{criteria}"
            text_of = bullet["text"].strip()
            marker = _MARKER.match(text_of)
            criterion: dict[str, object] = {
                "type": "criterion",
                "id": ident,
                "story": story["id"],
                "text": marker["text"].strip() if marker else text_of,
            }
            if marker and marker["mark"]:
                criterion["verification"] = marker["mark"].strip()
            objects[ident] = criterion
            continue

    for ident, numbers in lines_of.items():
        if len(numbers) > 1:
            listed = ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"
            problems.append(Problem(ident, f"defined more than once, lines {listed}"))
    for ident, item in objects.items():
        problems.extend(validate_object(item, ident))
    if problems:
        raise ContractError(problems)
    return list(objects.values())
