"""Reading the configuration file (US-E11-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zephyros.adapters.config_file import load_config
from zephyros.core.config import ConfigError

DIGEST = "ghcr.io/example/app@sha256:" + "c" * 64
VALID = f"""
schema_version = "1.0.0"

[images]
instance = "{DIGEST}"
guard = "{DIGEST}"

[hub]
agent_port = 9090
"""


def test_a_conforming_file_starts_the_hub(tmp_path: Path) -> None:
    path = tmp_path / "zephyros.toml"
    path.write_text(VALID, encoding="utf-8")
    config = load_config(path)
    assert config.hub.agent_port == 9090


def test_a_violating_file_is_refused_and_names_each_key(tmp_path: Path) -> None:
    path = tmp_path / "zephyros.toml"
    path.write_text(
        VALID.replace("9090", "80") + "\n[limits]\nmemory_mib = 'many'\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert "hub.agent_port" in message
    assert "limits.memory_mib" in message


def test_a_file_that_is_not_toml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "zephyros.toml"
    path.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "absent.toml")
