#!/bin/sh
# One process, no supervisor. A turn is `turn <prompt>`: OpenCode runs the prompt in the session
# of the instance, which the first turn creates, then exits (ADR 0016, ADR 0029). The session is
# found in OpenCode's database whatever project OpenCode names /work after (session.py); without
# one, `--continue` takes the last session of the work directory, or creates it.
# The prompt reaches OpenCode on its standard input, never on its command line: there, OpenCode
# would read a prompt that begins with "-" as an option, and quote one that holds a space.
# Any other command line goes to OpenCode as it is (`--version`, `debug config`).
set -eu
if [ "$#" -eq 2 ] && [ "$1" = "turn" ]; then
  : "${ZEPHYROS_HUB_URL:?}" "${ZEPHYROS_MODEL:?}" "${ZEPHYROS_TOKEN:?}"
  prompt=$2
  session=$(python3 -I /opt/zephyros/session.py) || session=""
  case "$session" in
    ses_*) set -- --session "$session" ;;
    *) set -- --continue ;;
  esac
  exec /opt/opencode/bin/opencode run "$@" <<PROMPT
$prompt
PROMPT
fi
exec /opt/opencode/bin/opencode "$@"
