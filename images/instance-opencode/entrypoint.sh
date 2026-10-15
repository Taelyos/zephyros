#!/bin/sh
# One process, no supervisor. A turn is `turn <prompt>`: OpenCode runs the prompt in the session
# of the instance, which the first turn creates, then exits (ADR 0016, ADR 0029). The session is
# found in OpenCode's database whatever project OpenCode names /work after (session.py); without
# one, `--continue` takes the last session of the work directory, or creates it. A turn the
# operator starts anew (ZEPHYROS_SESSION=new, ADR 0016) runs without either, so OpenCode creates
# a session, which the next turns then find as the last one written.
# The prompt reaches OpenCode on its standard input, never on its command line: there, OpenCode
# would read a prompt that begins with "-" as an option, and quote one that holds a space.
# Any other command line goes to OpenCode as it is (`--version`, `debug config`).
set -eu
# The hub says whether the model wants its reasoning back (ADR 0032); the configuration names
# the field that carries it, or none.
case "${ZEPHYROS_REASONING_ECHO:-false}" in
  true) ZEPHYROS_REASONING_FIELD=reasoning_content ;;
  *) ZEPHYROS_REASONING_FIELD= ;;
esac
# The window of the model and where to compact (ADR 0033), in tokens, from the hub; without
# them, the defaults of the hub. OpenCode compacts when a call reaches its input limit less the
# answer it reserves (16 000 tokens, the output of the model): the limit is set that much above.
case "${ZEPHYROS_CONTEXT_WINDOW:-}" in
  '' | *[!0-9]*) ZEPHYROS_CONTEXT_WINDOW=256000 ;;
esac
case "${ZEPHYROS_COMPACTION_TOKENS:-}" in
  '' | *[!0-9]*) ZEPHYROS_COMPACTION_TOKENS=224000 ;;
esac
ZEPHYROS_OPENCODE_INPUT=$((ZEPHYROS_COMPACTION_TOKENS + 16000))
export ZEPHYROS_REASONING_FIELD ZEPHYROS_CONTEXT_WINDOW ZEPHYROS_COMPACTION_TOKENS
export ZEPHYROS_OPENCODE_INPUT
if [ "$#" -eq 2 ] && [ "$1" = "turn" ]; then
  : "${ZEPHYROS_HUB_URL:?}" "${ZEPHYROS_MODEL:?}" "${ZEPHYROS_TOKEN:?}"
  prompt=$2
  if [ "${ZEPHYROS_SESSION:-continue}" = "new" ]; then
    set --
  else
    session=$(python3 -I /opt/zephyros/session.py) || session=""
    case "$session" in
      ses_*) set -- --session "$session" ;;
      *) set -- --continue ;;
    esac
  fi
  exec /opt/opencode/bin/opencode run "$@" <<PROMPT
$prompt
PROMPT
fi
exec /opt/opencode/bin/opencode "$@"
