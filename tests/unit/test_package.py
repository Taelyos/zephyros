"""The package version is single-sourced with pyproject.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

import zephyros


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert zephyros.__version__ == declared
