"""Version numbers of the public interfaces."""

from __future__ import annotations

import pytest

from zephyros.core.versioning import parse_version


def test_parses_three_numbers() -> None:
    assert parse_version("0.1.0") == (0, 1, 0)
    assert parse_version("12.0.345") == (12, 0, 345)


@pytest.mark.parametrize(
    "text",
    ["", "1", "1.2", "1.2.3.4", "01.2.3", "1.2.x", "v1.2.3", "1.2.3-rc1", " 1.2.3", "latest"],
)
def test_rejects_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="not a version number"):
        parse_version(text)
