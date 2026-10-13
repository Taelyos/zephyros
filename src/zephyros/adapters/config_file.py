"""Reads the configuration file of the hub (TOML) and validates it."""

from __future__ import annotations

import tomllib
from pathlib import Path

from zephyros.core.config import Config, ConfigError, parse_config
from zephyros.core.schema import Problem


def load_config(path: Path) -> Config:
    """Read `path` and return the configuration, or raise ConfigError naming each faulty key."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            [Problem("", f"cannot read {path}: {error.strerror or error}")]
        ) from error
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError([Problem("", f"{path} is not valid TOML: {error}")]) from error
    return parse_config(data)
