"""US-E11-04 (CTR-09): the hub must cover 85 % of its branches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tools import check_branch_coverage


def _report(**files: tuple[int, int]) -> dict[str, Any]:
    """A coverage JSON report. Each file maps its path to (covered, total) branches."""
    return {
        "files": {
            path.replace("__", "/"): {
                "summary": {"covered_branches": covered, "num_branches": total}
            }
            for path, (covered, total) in files.items()
        }
    }


HUB = "src__zephyros__a.py"


class TestCheck:
    def test_accepts_exactly_the_minimum(self) -> None:
        assert check_branch_coverage.check(_report(**{HUB: (85, 100)})) == []

    def test_rejects_one_branch_below(self) -> None:
        findings = check_branch_coverage.check(_report(**{HUB: (84, 100)}))
        assert len(findings) == 1
        assert "84/100" in findings[0]

    def test_a_hub_without_branches_passes(self) -> None:
        assert check_branch_coverage.check(_report(**{HUB: (0, 0)})) == []

    def test_adds_up_the_files_of_the_hub(self) -> None:
        report = _report(**{HUB: (10, 10), "src__zephyros__b.py": (0, 10)})
        assert check_branch_coverage.check(report) != []

    def test_ignores_files_outside_the_hub(self) -> None:
        report = _report(**{HUB: (10, 10), "tools__x.py": (0, 100)})
        assert check_branch_coverage.check(report) == []

    def test_rejects_a_report_without_hub_files(self) -> None:
        findings = check_branch_coverage.check(_report(**{"tools__x.py": (10, 10)}))
        assert any("coverage source is wrong" in f for f in findings)
        assert check_branch_coverage.check({}) != []


class TestMain:
    def test_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        good = tmp_path / "good.json"
        good.write_text(json.dumps(_report(**{HUB: (9, 10)})), encoding="utf-8")
        assert check_branch_coverage.main([str(good)]) == 0
        assert "9/10" in capsys.readouterr().out
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(_report(**{HUB: (1, 10)})), encoding="utf-8")
        assert check_branch_coverage.main([str(bad)]) == 1

    def test_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert check_branch_coverage.main([]) == 2
        assert "usage" in capsys.readouterr().out
