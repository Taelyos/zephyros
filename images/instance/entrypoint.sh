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
# The window of the model and where to compact (ADR 0033), in tokens, from the hub; a turn
# started without them (by hand) takes the defaults of the hub.
case "${ZEPHYROS_CONTEXT_WINDOW:-}" in
  '' | *[!0-9]*) ZEPHYROS_CONTEXT_WINDOW=256000 ;;
esac
case "${ZEPHYROS_COMPACTION_TOKENS:-}" in
  '' | *[!0-9]*) ZEPHYROS_COMPACTION_TOKENS=224000 ;;
esac
export ZEPHYROS_REASONING_ECHO ZEPHYROS_CONTEXT_WINDOW ZEPHYROS_COMPACTION_TOKENS
# A new session, when the operator asked for one (ADR 0016): the next `zephyros #N`, which the
# command of the turn then continues.
if [ "${ZEPHYROS_SESSION:-continue}" = "new" ]; then
  /opt/hermes/.venv/bin/python -I /opt/zephyros/new_session.py
fi
exec /opt/hermes/.venv/bin/hermes "$@"
