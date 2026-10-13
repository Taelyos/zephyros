"""US-E11-03 (CTR-08): suppressions need a code and a reason; strict mode stays on."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools import check_ignores

# The text is built from parts so that this file contains no suppression comment itself.
IGNORE = "# type" + ": ignore"
INLINE_CONFIG = "# mypy" + ": allow-untyped-defs"


def _findings(source: str) -> list[str]:
    return check_ignores.check_source("x.py", source)


class TestSuppressions:
    def test_accepts_code_and_reason(self) -> None:
        assert _findings(f"x = f()  {IGNORE}[no-any-return]  # reason: stub lacks a type\n") == []

    def test_accepts_several_codes(self) -> None:
        assert _findings(f"x = f()  {IGNORE}[attr-defined, misc]  # reason: generated\n") == []

    def test_rejects_missing_code(self) -> None:
        assert any("without an error code" in f for f in _findings(f"x = f()  {IGNORE}\n"))

    def test_rejects_missing_reason(self) -> None:
        assert any("without a reason" in f for f in _findings(f"x = f()  {IGNORE}[misc]\n"))

    def test_rejects_empty_reason(self) -> None:
        assert any(
            "without a reason" in f for f in _findings(f"x = f()  {IGNORE}[misc]  # reason:\n")
        )

    def test_rejects_pyright_style_without_reason(self) -> None:
        assert _findings("x = f()  # pyright" + ": ignore[reportGeneralTypeIssues]\n") != []

    def test_ignores_the_text_inside_strings(self) -> None:
        assert _findings(f'x = "{IGNORE}"\n') == []

    def test_reports_the_line_number(self) -> None:
        findings = _findings(f"a = 1\nb = f()  {IGNORE}\n")
        assert findings == ["x.py:2: suppression without an error code"]


class TestOtherWaysToSwitchTheCheckerOff:
    @pytest.mark.parametrize(
        "comment",
        [INLINE_CONFIG, "# mypy" + ": ignore-errors", "#mypy" + ": disable-error-code=misc"],
    )
    def test_rejects_every_inline_mypy_comment(self, comment: str) -> None:
        findings = _findings(f"{comment}\ndef f(x):\n    return x\n")
        assert findings == ["x.py:1: inline mypy configuration comment"]

    @pytest.mark.parametrize(
        "decorator",
        ["@no_type_check", "@typing.no_type_check", "@no_type_check()"],
    )
    def test_rejects_no_type_check(self, decorator: str) -> None:
        findings = _findings(f"{decorator}\ndef f(x):\n    return x\n")
        assert findings == ["x.py:1: no_type_check switches the type checker off"]

    def test_rejects_no_type_check_on_a_class_and_an_async_function(self) -> None:
        source = "@no_type_check\nclass A:\n    pass\n@no_type_check\nasync def g():\n    pass\n"
        assert len(_findings(source)) == 2

    def test_other_decorators_are_fine(self) -> None:
        assert _findings("@staticmethod\n@functools.cache\ndef f() -> None:\n    pass\n") == []


class TestConfiguration:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "pyproject.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_accepts_strict_mode_and_the_allowed_options(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "[tool.mypy]\nstrict = true\npython_version = '3.12'\nfiles = ['src']\n"
            "warn_unused_ignores = true\nenable_error_code = ['truthy-bool']\n"
            "[[tool.mypy.overrides]]\nmodule = 'a.*'\n",
        )
        assert check_ignores.check_mypy_config(path) == []

    def test_rejects_missing_strict(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "[tool.mypy]\npython_version = '3.12'\n")
        assert any("strict mode is not enabled" in f for f in check_ignores.check_mypy_config(path))

    @pytest.mark.parametrize(
        "line",
        [
            "disallow_untyped_defs = false",
            "disable_error_code = ['misc']",
            "exclude = ['src/zephyros/core']",
            "follow_imports = 'skip'",
            "ignore_missing_imports = true",
            "ignore_errors = true",
        ],
    )
    def test_rejects_a_weakening_global_option(self, tmp_path: Path, line: str) -> None:
        path = self._write(tmp_path, f"[tool.mypy]\nstrict = true\n{line}\n")
        key = line.split(" ")[0]
        assert any(
            f"option {key} is not allowed" in f for f in check_ignores.check_mypy_config(path)
        )

    @pytest.mark.parametrize(
        "line",
        ["disallow_untyped_defs = false", "ignore_errors = true", "ignore_missing_imports = true"],
    )
    def test_rejects_any_option_in_an_override(self, tmp_path: Path, line: str) -> None:
        path = self._write(
            tmp_path,
            f"[tool.mypy]\nstrict = true\n[[tool.mypy.overrides]]\nmodule = 'a'\n{line}\n",
        )
        key = line.split(" ")[0]
        findings = check_ignores.check_mypy_config(path)
        assert any(f"override option {key} is not allowed" in f for f in findings)

    def test_rejects_a_second_configuration_file(self, tmp_path: Path) -> None:
        assert check_ignores.check_other_config_files(tmp_path) == []
        (tmp_path / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
        (tmp_path / "setup.cfg").write_text("[mypy]\nignore_errors = True\n", encoding="utf-8")
        assert len(check_ignores.check_other_config_files(tmp_path)) == 2

    def test_a_setup_cfg_without_mypy_is_fine(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text("[flake8]\nmax-line-length = 100\n", encoding="utf-8")
        assert check_ignores.check_other_config_files(tmp_path) == []


class TestMain:
    def test_scans_the_source_directories(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert check_ignores.main([str(tmp_path)]) == 0
        (tmp_path / "src" / "a.py").write_text(f"x = f()  {IGNORE}\n", encoding="utf-8")
        assert check_ignores.main([str(tmp_path)]) == 1
        assert "src/a.py:1" in capsys.readouterr().out
