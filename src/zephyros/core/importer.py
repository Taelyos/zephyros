"""Importing a contract from the Markdown document of a project (Q-09: the Markdown importer
is Z3).

The document is written the way the method asks (requirements writing guide, section 5):
requirement and constraint tables, one row per object; story sections, one heading per story
(`#### US-Enn-nn · Epic Enn`), the story's sentence, a `Covers:` line, and the acceptance
criteria as bullets. The importer reads those four shapes and nothing else: the rest of the
document is prose and stays prose.
"""

from __future__ import annotations

import re

_METHODS = {"T": "test", "I": "inspection", "A": "analysis", "D": "demonstration"}

_ROW = re.compile(
    r"^\|\s*(?P<id>(?:EXG|CTR)-[0-9]{1,3})\s*\|\s*(?P<text>.+?)\s*\|\s*(?P<verification>.+?)\s*\|"
    r"\s*(?P<priority>[A-Z][0-9])\s*\|(?P<rationale>.*?)\|\s*$"
)
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
_IDS = re.compile(r"\b((?:EXG|CTR)-[0-9]{1,3})\b")


def _methods(verification: str) -> list[str]:
    found: list[str] = []
    for letter in _PARENTHETICAL.sub(" ", verification):
        if letter in _METHODS and _METHODS[letter] not in found:
            found.append(_METHODS[letter])
    return found


def import_markdown(text: str) -> list[dict[str, object]]:
    """The contract objects the document holds, in the order of the document: requirements and
    constraints from the tables, then each story with its criteria and its links. A row or a
    bullet shaped like the guide but invalid (a missing field, a broken identifier) is kept as
    is: the contract schema judges it, and the refusal names it."""
    objects: dict[str, dict[str, object]] = {}
    story: dict[str, object] | None = None
    criteria = 0

    for line in text.splitlines():
        stripped = line.strip()
        row = _ROW.match(stripped)
        if row:
            ident = row["id"]
            objects[ident] = {
                "type": "requirement" if ident.startswith("EXG") else "constraint",
                "id": ident,
                "text": row["text"].strip(),
                "priority": row["priority"],
                "methods": _methods(row["verification"]),
            }
            continue
        heading = _STORY.match(stripped)
        if heading:
            story = {"type": "story", "id": heading["id"], "epic": heading["epic"]}
            objects[heading["id"]] = story
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

    return list(objects.values())
