"""The core imports only the standard library and itself (design, section 3.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools import check_core_purity


def _check(
    source: str, module: str = "zephyros.core.a", package: str = "zephyros.core"
) -> list[str]:
    return check_core_purity.check_source("a.py", source, module, package)


class TestImports:
    @pytest.mark.parametrize(
        "source",
        [
            "import json\nimport os.path\n",
            "from dataclasses import dataclass\n",
            "from __future__ import annotations\n",
            "import zephyros.core.b\n",
            "from zephyros.core import b\n",
            "from . import b\n",
            "from .b import c\n",
        ],
    )
    def test_accepts(self, source: str) -> None:
        assert _check(source) == []

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("import pydantic\n", "pydantic"),
            ("from httpx import Client\n", "httpx"),
            ("import zephyros.adapters.docker\n", "zephyros.adapters.docker"),
            ("from zephyros.adapters import x\n", "zephyros.adapters"),
            ("from .. import adapters\n", "zephyros"),
            ("from ..adapters import x\n", "zephyros.adapters"),
        ],
    )
    def test_rejects(self, source: str, target: str) -> None:
        findings = _check(source)
        assert len(findings) == 1
        assert target in findings[0]

    def test_rejects_a_relative_import_that_leaves_the_tree(self) -> None:
        findings = _check("from .... import x\n")
        assert any("leaves the package tree" in f for f in findings)

    def test_relative_import_from_a_package_init(self) -> None:
        # In zephyros/core/sub/__init__.py, `from .. import b` is zephyros.core.b.
        assert _check("from .. import b\n", "zephyros.core.sub", "zephyros.core.sub") == []


class TestRun:
    def _tree(self, tmp_path: Path, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = tmp_path / "src" / "zephyros" / "core" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_clean_core(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._tree(tmp_path, {"__init__.py": '"""core"""\n', "a.py": "import json\n"})
        assert check_core_purity.main([str(tmp_path)]) == 0
        assert "only the standard library" in capsys.readouterr().out

    def test_dirty_core(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._tree(
            tmp_path, {"__init__.py": "", "sub/__init__.py": "", "sub/a.py": "import yaml\n"}
        )
        assert check_core_purity.main([str(tmp_path)]) == 1
        assert "zephyros.core.sub.a imports yaml" in capsys.readouterr().out
