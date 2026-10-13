"""The command line: `zephyros config check` and `zephyros config schema`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zephyros.app.cli import main

DIGEST = "ghcr.io/example/app@sha256:" + "d" * 64


def test_check_accepts_a_conforming_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "z.toml"
    path.write_text(
        f'schema_version = "1.0.0"\n[images]\ninstance = "{DIGEST}"\nguard = "{DIGEST}"\n',
        encoding="utf-8",
    )
    assert main(["config", "check", str(path)]) == 0
    assert "valid" in capsys.readouterr().out


def test_check_refuses_a_faulty_file_and_names_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "z.toml"
    path.write_text('schema_version = "1.0.0"\n[hub]\nagent_port = 1\n', encoding="utf-8")
    assert main(["config", "check", str(path)]) == 2
    error = capsys.readouterr().err
    assert "hub.agent_port" in error
    assert "images" in error


def test_schema_prints_the_published_document(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "schema"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["x-zephyros-schema-version"] == "1.12.0"
