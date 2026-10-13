#!/usr/bin/env bash
# Runs every check of the CI, in the same order. This is the one local command.
#
#   scripts/check.sh
#
# Needs `uv` on the PATH (see docs/development.md) and network access for the
# vulnerability audit. The CI runs this very script.
set -euo pipefail
cd "$(dirname "$0")/.."

# A host that exports PYTHONPATH (a frozen agent installation, a wrapper script) would leak
# its site-packages into the toolchain: every step runs with a clean one.
unset PYTHONPATH PYTHONHOME

step() { printf '\n==> %s\n' "$*"; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

step "Lock file is up to date"
uv lock --check

step "Install the locked dependencies"
uv sync --locked

step "Dependencies, images and actions are pinned by hash (CTR-10)"
uv run --locked python -m tools.check_pins

step "Type-checker suppressions have a code and a reason (CTR-08)"
uv run --locked python -m tools.check_ignores

step "The core imports only the standard library"
uv run --locked python -m tools.check_core_purity

step "Import contracts"
uv run --locked lint-imports

step "The published schemas are up to date (EXG-50)"
uv run --locked python -m tools.generate_schemas --check

step "Lint"
uv run --locked ruff check .

step "Format"
uv run --locked ruff format --check .

step "Types, strict mode (CTR-08)"
uv run --locked mypy

step "Tests, with coverage of the hub (CTR-09)"
uv run --locked pytest --cov --cov-report=term-missing --cov-report="json:$work/coverage.json"

step "At least 85 % of the branches of the hub are covered (CTR-09)"
uv run --locked python -m tools.check_branch_coverage "$work/coverage.json"

step "Vulnerable dependencies (CTR-09)"
uv export --locked --all-groups --no-emit-project --format requirements-txt \
  --output-file "$work/requirements.txt" >/dev/null
uv run --locked pip-audit --requirement "$work/requirements.txt" --require-hashes --disable-pip

step "Secrets in tracked files (CTR-09)"
# Tracked files and files not yet added, so that a local run sees what the CI will see.
git ls-files -z --cached --others --exclude-standard | xargs -0 uv run --locked detect-secrets-hook --baseline .secrets.baseline

printf '\nAll checks passed.\n'
