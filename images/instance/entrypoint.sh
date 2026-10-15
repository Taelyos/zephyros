#!/bin/sh
# One process, no supervisor: seed the configuration, then become Hermes.
set -eu
cp /opt/zephyros/seed.yaml "${HERMES_HOME:?}/config.yaml"
# The hub says true or false (ADR 0032). Hermes reads model.reasoning_echo as the truth of the
# text its managed scope expands, where "false" would read as true, and an unset variable too:
# the value it expands is "true" or empty.
case "${ZEPHYROS_REASONING_ECHO:-false}" in
  true) ZEPHYROS_REASONING_ECHO=true ;;
  *) ZEPHYROS_REASONING_ECHO= ;;
esac
export ZEPHYROS_REASONING_ECHO
exec /opt/hermes/.venv/bin/hermes "$@"
