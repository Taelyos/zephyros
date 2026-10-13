#!/bin/sh
# One process, no supervisor: seed the configuration, then become Hermes.
set -eu
cp /opt/zephyros/seed.yaml "${HERMES_HOME:?}/config.yaml"
exec /opt/hermes/.venv/bin/hermes "$@"
