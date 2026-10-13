"""CTR-13: the public API follows semantic versioning, and the CHANGELOG announces the
breaking changes. The surfaces of annex "Public API" each carry a version number."""

from __future__ import annotations

from pathlib import Path

from zephyros import __version__
from zephyros.core.config import CONFIG_SCHEMA_VERSION
from zephyros.core.contract import CONTRACT_SCHEMA_VERSION
from zephyros.core.webhooks import EVENTS_SCHEMA_VERSION

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


def test_every_public_schema_carries_a_semantic_version() -> None:
    for version in (CONFIG_SCHEMA_VERSION, CONTRACT_SCHEMA_VERSION, EVENTS_SCHEMA_VERSION):
        parts = version.split(".")
        assert len(parts) == 3, version
        assert all(part.isdigit() for part in parts), version


def test_the_changelog_exists_and_states_the_policy() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert text.startswith("# Changelog")
    assert "semantic versioning" in text
    assert "Breaking changes" in text  # the heading that announces them, before 1.0
    assert "## [Unreleased]" in text


def test_the_changelog_names_the_version_of_each_schema() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert CONFIG_SCHEMA_VERSION in text
    assert CONTRACT_SCHEMA_VERSION in text
    assert EVENTS_SCHEMA_VERSION in text


def test_the_version_is_the_pre_one_oh_milestone() -> None:
    # CTR-13: before 1.0, breaking changes are announced in the CHANGELOG; the first
    # milestone keeps the 0.x promise honest.
    assert __version__.startswith("0.")
