"""Reject type-checker suppressions without a reason, and a weakened strict mode.

Covers CTR-08 and US-E11-03. A suppression must name an error code and carry a
non-empty reason in the same comment::

    value = call()  # type: ignore[no-any-return]  # reason: the stub lacks a type

Other ways to silence or weaken the checker are never accepted: inline ``# mypy:``
configuration comments, ``@no_type_check``, a second mypy configuration file, and any
option in ``[tool.mypy]`` that is not on a short allow-list.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
import tomllib
from pathlib import Path

SCANNED_DIRS = ("src", "tools", "tests")

_SUPPRESSION = re.compile(r"(?:type:\s*ignore|pyright:\s*ignore)")
_WITH_CODE = re.compile(r"(?:type|pyright):\s*ignore\[[A-Za-z0-9_\-, ]+\]")
_REASON = re.compile(r"#\s*reason:\s*\S")
_INLINE_CONFIG = re.compile(r"^#\s*mypy:")

# Options of [tool.mypy] that cannot weaken the check. Everything else is refused.
ALLOWED_MYPY_OPTIONS = frozenset(
    {"strict", "python_version", "files", "warn_unused_ignores", "enable_error_code", "overrides"}
)
# An override may only select modules; it may set no option.
ALLOWED_OVERRIDE_OPTIONS = frozenset({"module"})
OTHER_MYPY_CONFIG_FILES = ("mypy.ini", ".mypy.ini")


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def check_source(path: str, source: str) -> list[str]:
    """Findings for the comments and decorators of one Python source."""
    findings: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        line = token.start[0]
        text = token.string
        if _INLINE_CONFIG.search(text):
            findings.append(f"{path}:{line}: inline mypy configuration comment")
        elif _SUPPRESSION.search(text):
            if not _WITH_CODE.search(text):
                findings.append(f"{path}:{line}: suppression without an error code")
            elif not _REASON.search(text):
                findings.append(f"{path}:{line}: suppression without a reason")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            findings.extend(
                f"{path}:{decorator.lineno}: no_type_check switches the type checker off"
                for decorator in node.decorator_list
                if _decorator_name(decorator) == "no_type_check"
            )
    return sorted(findings, key=lambda finding: int(finding.split(":")[1]))


def check_mypy_config(pyproject: Path) -> list[str]:
    """Strict mode is on, and nothing in the configuration weakens it."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    mypy = data.get("tool", {}).get("mypy", {})
    findings: list[str] = []
    if mypy.get("strict") is not True:
        findings.append(f"{pyproject}:1: mypy strict mode is not enabled")
    findings.extend(
        f"{pyproject}:1: mypy option {key} is not allowed"
        for key in sorted(set(mypy) - ALLOWED_MYPY_OPTIONS)
    )
    for override in mypy.get("overrides", []):
        findings.extend(
            f"{pyproject}:1: mypy override option {key} is not allowed"
            for key in sorted(set(override) - ALLOWED_OVERRIDE_OPTIONS)
        )
    return findings


def check_other_config_files(root: Path) -> list[str]:
    """No second mypy configuration that could take the place of pyproject.toml."""
    findings = [
        f"{root / name}:1: a second mypy configuration file"
        for name in OTHER_MYPY_CONFIG_FILES
        if (root / name).exists()
    ]
    setup_cfg = root / "setup.cfg"
    if setup_cfg.exists() and "[mypy" in setup_cfg.read_text(encoding="utf-8"):
        findings.append(f"{setup_cfg}:1: a second mypy configuration in setup.cfg")
    return findings


def run_all(root: Path) -> list[str]:
    findings = check_mypy_config(root / "pyproject.toml")
    findings.extend(check_other_config_files(root))
    for directory in SCANNED_DIRS:
        for path in sorted((root / directory).rglob("*.py")):
            relative = str(path.relative_to(root))
            findings.extend(check_source(relative, path.read_text(encoding="utf-8")))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    findings = run_all(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} problem(s) with the type-checker configuration or suppressions.")
        return 1
    print("Strict mode is on and every suppression has a code and a reason.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
