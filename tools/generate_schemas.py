"""Generate the published schemas into `schemas/` and the generated documents into `docs/`,
or check that they are up to date.

    python -m tools.generate_schemas           write the files
    python -m tools.generate_schemas --check   fail when a committed file differs

The schemas come from the code, so a change to the code that is not followed by a
regeneration fails the CI (EXG-50: the schema is published with a version number).
"""

from __future__ import annotations

import sys
from pathlib import Path

from zephyros.core.publication import published_documents, published_schemas

SCHEMAS_DIR = "schemas"


def check(root: Path) -> list[str]:
    """Findings: a published schema that is missing or differs, and a file that no schema owns."""
    findings: list[str] = []
    expected = published_schemas()
    directory = root / SCHEMAS_DIR
    for name, text in expected.items():
        path = directory / name
        if not path.exists():
            findings.append(f"{SCHEMAS_DIR}/{name}: missing; run python -m tools.generate_schemas")
        elif path.read_text(encoding="utf-8") != text:
            findings.append(
                f"{SCHEMAS_DIR}/{name}: differs from the code; run python -m tools.generate_schemas"
            )
    for relative, text in published_documents().items():
        document = root / relative
        if not document.exists():
            findings.append(f"{relative}: missing; run python -m tools.generate_schemas")
        elif document.read_text(encoding="utf-8") != text:
            findings.append(
                f"{relative}: differs from the code; run python -m tools.generate_schemas"
            )
    if directory.is_dir():
        findings.extend(
            f"{SCHEMAS_DIR}/{path.name}: not produced by any schema of the code"
            for path in sorted(directory.glob("*.json"))
            if path.name not in expected
        )
    return findings


def write(root: Path) -> list[Path]:
    directory = root / SCHEMAS_DIR
    directory.mkdir(exist_ok=True)
    written = []
    for name, text in published_schemas().items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    for relative, text in published_documents().items():
        document = root / relative
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(text, encoding="utf-8")
        written.append(document)
    return written


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path.cwd()
    if args == ["--check"]:
        findings = check(root)
        for finding in findings:
            print(finding)
        if findings:
            return 1
        print("The published schemas are up to date.")
        return 0
    if args:
        print("usage: python -m tools.generate_schemas [--check]", file=sys.stderr)
        return 2
    for path in write(root):
        print(f"wrote {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
