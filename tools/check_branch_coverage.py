"""Require that the tests of the hub cover at least 85 % of its branches.

Covers US-E11-04 and the continuous integration checks (CTR-09). coverage.py's own
``fail_under`` gate uses a combined figure of statements and branches; this check
reads the JSON report and uses branches only, for the hub (``src/zephyros``) alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MINIMUM_PERCENT = 85
HUB_PREFIX = "src/zephyros/"


def branch_totals(report: dict[str, Any], prefix: str = HUB_PREFIX) -> tuple[int, int, int]:
    """Return (files measured, covered branches, branches) for the files under `prefix`."""
    files = 0
    covered = 0
    total = 0
    for name, data in report.get("files", {}).items():
        if not str(name).replace("\\", "/").startswith(prefix):
            continue
        summary = data["summary"]
        files += 1
        covered += int(summary["covered_branches"])
        total += int(summary["num_branches"])
    return files, covered, total


def check(report: dict[str, Any], minimum: int = MINIMUM_PERCENT) -> list[str]:
    files, covered, total = branch_totals(report)
    if files == 0:
        return [f"no file under {HUB_PREFIX} was measured: the coverage source is wrong"]
    if covered * 100 < minimum * total:
        return [f"branch coverage of the hub is {covered}/{total}, below {minimum} %"]
    return []


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m tools.check_branch_coverage COVERAGE_JSON")
        return 2
    report = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    findings = check(report)
    for finding in findings:
        print(finding)
    if findings:
        return 1
    _, covered, total = branch_totals(report)
    print(f"Branch coverage of the hub: {covered}/{total} (minimum {MINIMUM_PERCENT} %).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
