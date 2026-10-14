#!/usr/bin/env bash
# Run Zephyros on this machine: build the images, write the configuration, start the hub and the
# container filter.
#
#   scripts/deploy.sh up      build and start (again: rebuilds what changed)
#   scripts/deploy.sh down [--volumes]   stop the hub and the filter; instances stay, and so do
#                           the hub's data and the filter's journal unless --volumes is given
#   scripts/deploy.sh call TOOL [JSON]   call a tool on the operator channel
#   scripts/deploy.sh rotate-token operator|main-agent   a new token in its file, the old one
#                           revoked (ADR 0026); the operator's is read by `call` at each call,
#                           the main agent's is given to the main agent again
#
# Environment (all optional):
#   ZEPHYROS_STATE          where the configuration is written (default: deploy/state)
#   ZEPHYROS_PROJECT        the compose project name (default: zephyros)
#   ZEPHYROS_HUB_NAME       the name of the hub container (default: zephyros-hub)
#   ZEPHYROS_OPERATOR_PORT  the operator port on 127.0.0.1 (default: 8081)
#   ZEPHYROS_MAIN_AGENT_PORT  the main-agent port on 127.0.0.1 (default: 8082)
#   ZEPHYROS_POOL           the range of the instance networks (default: 10.200.0.0/16)
#   ZEPHYROS_RESOLVERS      the name resolvers the instances use, comma-separated
#                           (default: 9.9.9.9, Quad9; a protected address is refused)
#   ZEPHYROS_EGRESS         what an instance reaches besides the hub: regulated (the default:
#                           the allowed hosts, through the egress proxy of the hub), sealed
#                           (the hub alone) or open (every public destination); ADR 0024
#   ZEPHYROS_ALLOWED_HOSTS  the hosts a regulated instance reaches, comma-separated (default:
#                           the package registries, see docs/configuration.md)
#   ZEPHYROS_KIT           the verification kit of the delivery loop (for example "tests");
#                           builds images/kit-tests and writes the [verification] table
#   ZEPHYROS_FORGE          "directory" opens pull requests in a directory of the host (the
#                           development deployment); "github" opens them on GitHub and needs
#                           ZEPHYROS_FORGE_TOKEN_FILE; anything else writes no [forge]
#   ZEPHYROS_FORGE_DIR      where the directory forge lives (default: $ZEPHYROS_STATE/forge)
#   ZEPHYROS_GATEWAY_KEY_FILE   the file of the provider key, mounted into the hub, read-only,
#                           at /run/zephyros-credentials/gateway.key (the configuration names
#                           that path); the file itself, never a copy
#   ZEPHYROS_FORGE_TOKEN_FILE   the file of the GitHub token, mounted the same way at
#                           /run/zephyros-credentials/forge.key
#   ZEPHYROS_EXTRA_CONFIG   TOML appended to the configuration (for example a [gateway] table)
set -euo pipefail
cd "$(dirname "$0")/.."

export ZEPHYROS_PROJECT="${ZEPHYROS_PROJECT:-zephyros}"
export ZEPHYROS_HUB_NAME="${ZEPHYROS_HUB_NAME:-zephyros-hub}"
export ZEPHYROS_OPERATOR_PORT="${ZEPHYROS_OPERATOR_PORT:-8081}"
export ZEPHYROS_MAIN_AGENT_PORT="${ZEPHYROS_MAIN_AGENT_PORT:-8082}"
export ZEPHYROS_RESOLVERS="${ZEPHYROS_RESOLVERS:-9.9.9.9}"
state="${ZEPHYROS_STATE:-deploy/state}"
compose=(docker compose -f deploy/compose.yaml)

# The directory forge must be an absolute, existing, host path before compose parses the file.
# The hub runs as uid 10001, so up() makes it world-writable when the forge is asked for — the
# development deployment, where a person inspects the pull requests by hand.
forge_dir="${ZEPHYROS_FORGE_DIR:-$state/forge}"
mkdir -p "$forge_dir"
export ZEPHYROS_FORGE_DIR="$(cd "$forge_dir" && pwd)"

# The credential files (the provider key, the GitHub token) are bind-mounted into the hub,
# read-only, at /run/zephyros-credentials — the files themselves, never a copy. When none is
# given, an empty file is mounted instead: the configuration only names the real one when the
# gateway or the forge asks for it. The hub runs as uid 10001, so a real file must be readable
# by exactly that uid: make_credential_readable adds that one read ACL, and nothing more.
mkdir -p "$state"
empty_credential="$(cd "$state" && pwd)/empty-credential"
[ -e "$empty_credential" ] || : > "$empty_credential"
export ZEPHYROS_GATEWAY_KEY_FILE="$(realpath "${ZEPHYROS_GATEWAY_KEY_FILE:-$empty_credential}")"
export ZEPHYROS_FORGE_TOKEN_FILE="$(realpath "${ZEPHYROS_FORGE_TOKEN_FILE:-$empty_credential}")"

make_credential_readable() {
  local file="$1"
  [ "$file" = "$empty_credential" ] && return 0
  if command -v setfacl >/dev/null 2>&1; then
    setfacl -m u:10001:r "$file"
  else
    echo "warning: setfacl is missing; the hub (uid 10001) may not read $file" >&2
  fi
}

image_id() { docker image inspect -f '{{.Id}}' "$1"; }

up() {
  mkdir -p "$state"
  docker build -q -t zephyros-guard:local images/guard >/dev/null
  docker build -q -t zephyros-instance:local images/instance >/dev/null
  docker build -q -f images/zephyros/Dockerfile -t zephyros:local . >/dev/null
  export ZEPHYROS_IMAGE; ZEPHYROS_IMAGE=$(image_id zephyros:local)
  # deploy/compose.yaml takes its image from this variable: it must be an image identifier.
  [[ "$ZEPHYROS_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "not an image identifier" >&2; exit 1; }
  export ZEPHYROS_CONFIG; ZEPHYROS_CONFIG="$(cd "$state" && pwd)/zephyros.toml"
  resolvers_toml="$(printf '%s' "$ZEPHYROS_RESOLVERS" | tr ',' '\n' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^/"/' -e 's/$/"/' \
    | paste -sd, -)"
  make_credential_readable "$ZEPHYROS_GATEWAY_KEY_FILE"
  # The schema version the code speaks, read from the code: never a copy that drifts.
  schema_version="$(sed -n 's/^CONFIG_SCHEMA_VERSION = "\([0-9.]*\)"$/\1/p' src/zephyros/core/config.py)"
  [[ "$schema_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "no schema version in the code" >&2; exit 1; }
  cat > "$ZEPHYROS_CONFIG" <<TOML
# Written by scripts/deploy.sh; images by their local identifier.
schema_version = "$schema_version"

[images]
guard = "$(image_id zephyros-guard:local)"
instance = "$(image_id zephyros-instance:local)"

[hub]
operator_bind = "core"
main_agent_bind = "core"

[runtime]
hub_container = "$ZEPHYROS_HUB_NAME"

[network]
instance_pool = "${ZEPHYROS_POOL:-10.200.0.0/16}"
resolvers = [$resolvers_toml]
egress = "${ZEPHYROS_EGRESS:-regulated}"
TOML
  if [ -n "${ZEPHYROS_ALLOWED_HOSTS:-}" ]; then
    hosts_toml="$(printf '%s' "$ZEPHYROS_ALLOWED_HOSTS" | tr ',' '\n' \
      | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^/"/' -e 's/$/"/' \
      | paste -sd, -)"
    printf 'allowed_hosts = [%s]\n' "$hosts_toml" >> "$ZEPHYROS_CONFIG"
  fi
  if [ -n "${ZEPHYROS_KIT:-}" ]; then
    docker build -q -t zephyros-kit-tests:local images/kit-tests >/dev/null
    cat >> "$ZEPHYROS_CONFIG" <<TOML

[verification]
runner = "sandbox"
kit = "$ZEPHYROS_KIT"
kit_image = "$(image_id zephyros-kit-tests:local)"
TOML
  fi
  if [ "${ZEPHYROS_FORGE:-none}" = "directory" ]; then
    # The hub (uid 10001) pushes into the bare repositories a person seeded as themselves:
    # development and tests only, where the forge is a directory of this machine.
    chmod -R a+rwX "$ZEPHYROS_FORGE_DIR"
    cat >> "$ZEPHYROS_CONFIG" <<TOML

[forge]
provider = "directory"
root = "/var/lib/zephyros/forge"
TOML
  fi
  if [ "${ZEPHYROS_FORGE:-none}" = "github" ]; then
    make_credential_readable "$ZEPHYROS_FORGE_TOKEN_FILE"
    cat >> "$ZEPHYROS_CONFIG" <<TOML

[forge]
provider = "github"
token_file = "/run/zephyros-credentials/forge.key"
TOML
  fi
  if [ -n "${ZEPHYROS_EXTRA_CONFIG:-}" ]; then printf '\n%s\n' "$ZEPHYROS_EXTRA_CONFIG" >> "$ZEPHYROS_CONFIG"; fi
  docker run --rm -v "$ZEPHYROS_CONFIG:/c.toml:ro" "$ZEPHYROS_IMAGE" config check /c.toml
  "${compose[@]}" up -d --wait
  echo "Zephyros is up: operator channel on 127.0.0.1:$ZEPHYROS_OPERATOR_PORT"
}

down() {
  export ZEPHYROS_IMAGE=unused ZEPHYROS_CONFIG=/dev/null
  # The filter creates the guards, turns and sandboxes of the instances, and their networks,
  # outside compose: the second real run's `down` left four guards behind. Stop the hub first,
  # so it starts nothing more, then remove them; their volumes stay, they hold the instances'
  # state, and the hub creates the networks again when it starts an instance.
  local managed=(--filter "label=org.zephyros.managed=true")
  "${compose[@]}" stop
  docker ps -aq "${managed[@]}" | xargs -r docker rm -f >/dev/null
  "${compose[@]}" down "$@"
  docker network ls -q "${managed[@]}" | xargs -r docker network rm >/dev/null
}

call() {
  docker exec "$ZEPHYROS_HUB_NAME" /opt/zephyros/bin/zephyros hub call "$1" --args "${2:-{\}}" \
    --url http://127.0.0.1:8081
}

rotate_token() {
  docker exec "$ZEPHYROS_HUB_NAME" /opt/zephyros/bin/zephyros hub rotate-token "$1"
}

case "${1:-}" in
  up) up ;;
  down) shift; down "$@" ;;
  call) shift; call "$@" ;;
  rotate-token) shift; rotate_token "${1:-}" ;;
  *) echo "usage: scripts/deploy.sh up|down|call TOOL [JSON]|rotate-token operator|main-agent" >&2
     exit 2 ;;
esac
