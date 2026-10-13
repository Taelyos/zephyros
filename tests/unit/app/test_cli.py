"""The command line: `zephyros config check` and `zephyros config schema`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zephyros.app.cli import main
from zephyros.core.config import CONFIG_SCHEMA_VERSION

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


GATEWAY = """
[gateway]
provider = "openai-compatible"
upstream_url = "https://provider.example.test/v1"
credential_file = "/run/zephyros-credentials/gateway.key"
model = "served-model"
upstream_model = "{served}"
models = ["deepseek-v4.1-flash"]
"""


def test_check_holds_a_gateway_to_the_models_its_file_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A [gateway] table that names one model alone authorises that one alone: the check
    that deploy.sh runs refuses any other, and says which key to fix and why."""
    path = tmp_path / "z.toml"
    header = f'schema_version = "{CONFIG_SCHEMA_VERSION}"\n'
    images = f'[images]\ninstance = "{DIGEST}"\nguard = "{DIGEST}"\n'
    path.write_text(header + images + GATEWAY.format(served="deepseek-v4.1-flash"), "utf-8")
    assert main(["config", "check", str(path)]) == 0
    capsys.readouterr()
    path.write_text(header + images + GATEWAY.format(served="claude-opus-5-5"), "utf-8")
    assert main(["config", "check", str(path)]) == 2
    assert (
        "gateway.upstream_model: 'claude-opus-5-5' is not one of gateway.models "
        "(deepseek-v4.1-flash): the gateway serves only the models its configuration names"
    ) in capsys.readouterr().err


def test_schema_prints_the_published_document(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "schema"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["x-zephyros-schema-version"] == CONFIG_SCHEMA_VERSION
