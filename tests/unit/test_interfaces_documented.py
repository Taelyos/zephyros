"""US-E11-06 (I): each public interface is documented, with the version that the code carries."""

from __future__ import annotations

import re
from pathlib import Path

from zephyros.core.ports import INTERFACE_VERSIONS

PAGE = Path(__file__).parents[2] / "docs" / "interfaces.md"
_SECTION = re.compile(r"^## (\w+)\n\nVersion: `([^`]+)`$", re.MULTILINE)


def test_the_page_gives_the_version_of_every_interface_as_the_code_does() -> None:
    documented = dict(_SECTION.findall(PAGE.read_text(encoding="utf-8")))
    assert documented == dict(INTERFACE_VERSIONS)


def test_every_section_lists_its_operations_checks_and_dummy() -> None:
    text = PAGE.read_text(encoding="utf-8")
    for name in INTERFACE_VERSIONS:
        section = text.split(f"## {name}\n", 1)[1].split("\n## ", 1)[0]
        assert "| Operation | Meaning |" in section, name
        assert "What the suite checks:" in section, name
        assert "Dummy: `" in section, name


def test_the_page_warns_that_the_interfaces_are_not_stable_yet() -> None:
    text = PAGE.read_text(encoding="utf-8")
    assert "version 0.x" in text
    assert "spike Z2-01" in text
