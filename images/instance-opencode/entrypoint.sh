#!/bin/sh
# One process, no supervisor. A turn is `turn <prompt>`: OpenCode runs the prompt in the last
# session of the work directory, which the first turn creates, then exits (ADR 0016, ADR 0029).
# The prompt reaches OpenCode on its standard input, never on its command line: there, OpenCode
# would read a prompt that begins with "-" as an option, and quote one that holds a space.
# Any other command line goes to OpenCode as it is (`--version`, `debug config`).
set -eu
if [ "$#" -eq 2 ] && [ "$1" = "turn" ]; then
  : "${ZEPHYROS_HUB_URL:?}" "${ZEPHYROS_MODEL:?}" "${ZEPHYROS_TOKEN:?}"
  exec /opt/opencode/bin/opencode run --continue <<PROMPT
$2
PROMPT
fi
exec /opt/opencode/bin/opencode "$@"
