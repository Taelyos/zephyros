"""Reject any import in the hub core other than the standard library and the core itself.

Covers the dependency rule of the Z2 design, section 3.2, which is also
the rule that CTR-11 asks for. Adapters-import-core is checked by import-linter;
this check adds that the core uses nothing third-party.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE = "zephyros.core"


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _resolve(node: ast.ImportFrom, package: str) -> str | None:
    """Absolute module name of a `from` import, or None when it leaves the package tree."""
    if node.level == 0:
        return node.module
    parts = package.split(".")
    if node.level - 1 >= len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    if node.module:
        base.append(node.module)
    return ".".join(base)


def check_source(path: str, source: str, module: str, package: str) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve(node, package)
            if resolved is None:
                findings.append(f"{path}:{node.lineno}: relative import leaves the package tree")
                continue
            targets = [resolved]
        else:
            continue
        for target in targets:
            top = target.split(".")[0]
            if top in sys.stdlib_module_names:
                continue
            if target == CORE or target.startswith(CORE + "."):
                continue
            findings.append(f"{path}:{node.lineno}: core module {module} imports {target}")
    return findings


def run_all(root: Path) -> list[str]:
    findings: list[str] = []
    core_dir = root / "src" / "zephyros" / "core"
    for path in sorted(core_dir.rglob("*.py")):
        module = _module_name(root, path)
        relative = str(path.relative_to(root))
        findings.extend(
            check_source(
                relative, path.read_text(encoding="utf-8"), module, _package_of(module, path)
            )
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    findings = run_all(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} import(s) of the core break the dependency rule.")
        return 1
    print("The core imports only the standard library and itself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
