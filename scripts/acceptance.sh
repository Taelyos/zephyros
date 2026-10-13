#!/usr/bin/env bash
# The tests that need a container runtime: the hardening of a turn and the network containment,
# on real containers with the real filter templates and the real network guard. They build the
# images they need, and remove every object they create.
#
#   scripts/acceptance.sh            the whole runtime suite
#   scripts/acceptance.sh -k atk_39  pytest options are passed through
#
# Needs a Docker-compatible runtime (OrbStack on macOS works) and `docker` on PATH. The socket is
# /var/run/docker.sock unless ZEPHYROS_DOCKER_SOCKET says otherwise.
set -euo pipefail
cd "$(dirname "$0")/.."

# A host that exports PYTHONPATH (a frozen agent installation, a wrapper script) would leak
# its site-packages into pytest: run with a clean one.
unset PYTHONPATH PYTHONHOME

docker version >/dev/null || { echo "no container runtime is reachable" >&2; exit 2; }
export ZEPHYROS_RUNTIME_TESTS=1
exec uv run --locked pytest tests/runtime -p no:cacheprovider -rs "$@"
