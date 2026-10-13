#!/usr/bin/env bash
# Spike Z2-01: the Hermes instance image.
#
#   spikes/z2-01/run.sh            run every experiment, print one line per result
#
# Throwaway code: it answers the questions of the Z2 design (section 9, "Spike Z2-01") and is
# never imported by src/. It needs a Docker-compatible runtime, bash and python3, and it runs
# unchanged on macOS (OrbStack) and on Linux (the manual CI job spike-z2-01).
#
# Method. Hermes never runs on the host. Every run is a container of the image built from
# image/hermes.Dockerfile, started with the container hardening profile, with its own home
# volume, against a fake hub (fake_hub.py) that serves the model endpoint and the MCP endpoint
# on one address. PASS and FAIL are expectations; INFO lines are measurements and findings.
set -u
cd "$(dirname "$0")"
SPK="$PWD"

LABEL="zs.spike=1"
PY="python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"
IMG="zs-hermes:spike"
SNIFFER="zs-sniffer:spike"
REVISION="818c13be1dc4fd28987e1e881a9408224afd4535" # pragma: allowlist secret
TOKEN_A="zph_c_spike-test-token-0000000000000000"
TOKEN_B="zph_c_spike-test-token-1111111111111111"
PASS=0
FAIL=0

step() { printf '\n== %s\n' "$*"; }
info() { printf 'INFO  %-6s %s\n' "$1" "$2"; }
verdict() { # id expected observed text
  if [ "$2" = "$3" ]; then
    printf 'PASS  %-6s %-8s %s\n' "$1" "$3" "$4"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-6s expected %s, observed %s: %s\n' "$1" "$2" "$3" "$4"
    FAIL=$((FAIL + 1))
  fi
}
yes_no() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }
has() { # string literal...   yes when every literal occurs in the string
  local s=$1 l
  shift
  for l in "$@"; do
    case "$s" in *"$l"*) ;; *) echo no; return ;; esac
  done
  echo yes
}
now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

CAPDIR=$(mktemp -d)
cleanup() {
  rm -rf "$CAPDIR"
  docker ps -aq --filter "label=$LABEL" | xargs -r docker rm -f >/dev/null 2>&1
  docker network ls -q --filter "label=$LABEL" | xargs -r docker network rm >/dev/null 2>&1
  docker volume ls -q --filter "label=$LABEL" | xargs -r docker volume rm >/dev/null 2>&1
  return 0
}
trap cleanup EXIT
cleanup

# ---- building blocks
HARDEN=(--init --label "$LABEL" --user 10000:10000 --read-only --cap-drop ALL
  --security-opt no-new-privileges --tmpfs /tmp --ipc=none --memory 1g --cpus 2 --pids-limit 512)
ENVS=(-e ZEPHYROS_HUB_URL=http://fake-hub:8080 -e ZEPHYROS_MODEL=fake-model)
HERMES=/opt/hermes/.venv/bin/hermes

vol() { docker volume create --label "$LABEL" "$1" >/dev/null; }

fake_start() { # scenario [ENV=VALUE ...]   (a fresh fake hub, hence a fresh log)
  local scenario=$1
  shift
  local extra=()
  local kv
  for kv in "$@"; do extra+=(-e "$kv"); done
  docker rm -f fake-hub >/dev/null 2>&1
  docker run -d --label "$LABEL" --name fake-hub --network zs01-net \
    -e "FAKE_SCENARIO=/spike/scenarios/$scenario.json" -e "FAKE_TOKEN=$TOKEN_A" -e "FAKE_TOKEN_NEW=$TOKEN_B" \
    ${extra[@]+"${extra[@]}"} -v "$SPK":/spike:ro "$PY" python -u /spike/fake_hub.py >/dev/null
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    docker logs fake-hub 2>&1 | grep -q '"kind": "ready"' && return 0
    sleep 0.5
  done
  return 1
}
fl() { docker logs fake-hub 2>&1 | python3 "$SPK/analyze.py" "$@"; }

# turn VOLUME [docker args...] -- hermes args...      image entrypoint: seed the config, then Hermes
TURN_OUT=""
TURN_MS=0
turn() {
  local home=$1
  shift
  local extra=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do extra+=("$1"); shift; done
  shift
  local t0
  t0=$(now_ms)
  TURN_OUT=$(docker run --rm "${HARDEN[@]}" --network "${NET:-zs01-net}" -v "$home":/opt/data -v "${WORK:-zs01-work}":/work \
    "${ENVS[@]}" -e "ZEPHYROS_TOKEN=${TOKEN:-$TOKEN_A}" ${extra[@]+"${extra[@]}"} "$IMG" "$@" 2>&1)
  TURN_MS=$(($(now_ms) - t0))
}

# variant VOLUME CONFIGFILE [docker args...] -- hermes args...     a configuration of the instance's own making
variant() {
  local home=$1 cfg=$2
  shift 2
  local extra=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do extra+=("$1"); shift; done
  shift
  local t0
  t0=$(now_ms)
  TURN_OUT=$(docker run --rm "${HARDEN[@]}" --network "${NET:-zs01-net}" -v "$home":/opt/data -v "${WORK:-zs01-work}":/work \
    -v "$SPK":/spike:ro "${ENVS[@]}" -e "ZEPHYROS_TOKEN=${TOKEN:-$TOKEN_A}" ${extra[@]+"${extra[@]}"} \
    --entrypoint sh "$IMG" -c "cp /spike/configs/$cfg /opt/data/config.yaml && exec $HERMES \"\$@\"" sh "$@" 2>&1)
  TURN_MS=$(($(now_ms) - t0))
}

# wait_for_chat COUNT: until the fake hub has seen COUNT chat requests (at most ~40 s)
wait_for_chat() {
  local i
  for i in $(seq 1 80); do
    [ "$(fl count chat)" -ge "$1" ] && return 0
    sleep 0.5
  done
  return 1
}

step "build"
docker build -q -f image/hermes.Dockerfile -t "$IMG" image >/dev/null || { echo "image build failed"; exit 1; }
docker build -q -f image/sniffer.Dockerfile -t "$SNIFFER" image >/dev/null || { echo "sniffer build failed"; exit 1; }
docker network create --label "$LABEL" zs01-net >/dev/null
vol zs01-work

# ------------------------------------------------------------------------------------------
step "H-00 runtime facts"
info H-00 "docker $(docker version --format '{{.Server.Version}} on {{.Server.Os}}/{{.Server.Arch}}'), kernel $(docker info --format '{{.KernelVersion}}'), client OS $(uname -s)"
info H-00 "memory visible to the runtime: $(docker info --format '{{.MemTotal}}') bytes, cpus: $(docker info --format '{{.NCPU}}')"

# ------------------------------------------------------------------------------------------
step "H-01 the pinned version"
base_digest=$(grep '^FROM' image/hermes.Dockerfile | head -1 | sed 's/.*@//')
revision=$(docker run --rm --entrypoint cat "$IMG" /etc/hermes/image-provenance.json 2>&1 | tr ',' '\n' | grep revision | sed 's/.*:"\(.*\)"/\1/')
version_line=$(docker run --rm --entrypoint "$HERMES" "$IMG" --version 2>&1 | head -1)
verdict H-01 yes "$([ "$revision" = "$REVISION" ] && echo yes || echo no)" "the image provenance file names commit $REVISION"
verdict H-01b yes "$(has "$version_line" "upstream ${REVISION:0:8}")" "hermes --version: $version_line"
verdict H-01c yes "$(has "$base_digest" "sha256:")" "the base image is referenced by digest"
info H-01 "base image digest (index): $base_digest"
info H-01 "image size: $(docker image inspect "$IMG" -f '{{.Size}}' | python3 -c 'import sys; print(round(int(sys.stdin.read()) / 1e6), "MB")')"
tools_present=$(docker run --rm --entrypoint sh "$IMG" -c 'for t in docker ssh git gcc chromium node; do p=$(command -v $t 2>/dev/null) && echo "$t=$p"; done' 2>&1 | tr '\n' ' ')
info H-01 "tools present in the image: $tools_present"

# ------------------------------------------------------------------------------------------
step "H-02 hardening profile and writable locations"
vol zs01-subject-home
docker run -d --name zs01-subject "${HARDEN[@]}" --network zs01-net -v zs01-subject-home:/opt/data -v zs01-work:/work \
  -v "$SPK":/spike:ro --entrypoint sleep "$IMG" 900 >/dev/null
sleep 1
inspect=$(docker inspect zs01-subject --format '{{.Config.User}}|{{.HostConfig.CapDrop}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.SecurityOpt}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.IpcMode}}')
info H-02 "container settings: $inspect"
verdict H-02 yes "$(has "$inspect" "10000:10000|[ALL]|true|[no-new-privileges]|false|")" "non-root, all capabilities dropped, read-only root, no-new-privileges, not privileged"
caps=$(docker exec zs01-subject python3 /spike/probe.py | python3 probe_fmt.py caps)
verdict H-02b "uid=10000 CapEff=0000000000000000 NoNewPrivs=1 Seccomp=2" "$caps" "seen from inside: user, effective capabilities, no-new-privileges, a syscall filter in force (the runtime default)"
refused=$(docker exec zs01-subject sh -c 'n=0; for f in /etc/hermes/x /opt/hermes/x /usr/bin/x /opt/zephyros/x; do touch $f 2>/dev/null || n=$((n+1)); done; echo $n')
verdict H-02c 4 "$refused" "writes to the Hermes install, the policy, /usr and the seed are refused"
writable=$(docker exec zs01-subject python3 /spike/writable.py | tr '\n' ' ')
info H-02 "writable locations with --ipc=none: $writable"
verdict H-02d "/dev/mqueue /opt/data /tmp /work " "$writable" "three writable locations, plus the runtime's /dev/mqueue (see the report)"
default_ipc=$(docker run --rm --init --user 10000:10000 --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp \
  --network none -v zs01-subject-home:/opt/data -v zs01-work:/work -v "$SPK":/spike:ro --entrypoint python3 "$IMG" /spike/writable.py | tr '\n' ' ')
info H-02 "writable locations with the default IPC mode: $default_ipc"
verdict H-02e yes "$(has "$default_ipc" "/dev/shm")" "control: the default IPC mode adds /dev/shm, --ipc=none removes it"
verdict H-02f yes "$(yes_no docker exec zs01-subject sh -c 'test ! -e /var/run/docker.sock && test ! -e /run/docker.sock')" "no container-runtime socket in the instance"
info H-02 "listeners at rest: $(docker exec zs01-subject python3 /spike/probe.py | python3 probe_fmt.py listeners) (the runtime's own DNS at 127.0.0.11 is not counted)"
docker rm -f zs01-subject >/dev/null

# ------------------------------------------------------------------------------------------
step "H-03 one agent turn against the fake hub"
vol zs01-home-a
fake_start basic
turn zs01-home-a -- -z "Say hello"
verdict H-03 "OK from the fake model." "$(printf '%s' "$TURN_OUT" | tail -1)" "a hardened instance completes a turn (${TURN_MS} ms from docker run to exit)"
info H-03 "requests by the instance, once each (count, method, path):"
fl paths | sed 's/^/        /'
verdict H-03b 1 "$(fl clients | tr ',' '\n' | grep -c .)" "every request comes from one source address (the instance)"
info H-03 "bearer on all requests: $(fl auth-summary)"
info H-03 "requests without a bearer: $(fl missing-paths)"
verdict H-03c 0 "$(fl bad-auth-outside-models)" "every request carries the bearer, except the GET /v1/models noted above"
info H-03 "model call: stream flags $(fl streams), stream_options $(fl stream-options), $(fl aux-chats) auxiliary chat call(s) without tools"
info H-03 "tools sent to the model ($(fl tool-count)): $(fl tool-names)"

# ------------------------------------------------------------------------------------------
step "H-04 MCP server with a bearer header, and how the tools appear"
fake_start mcp
turn zs01-home-a -- -z "Who am I?"
info H-04 "MCP exchange: $(fl mcp-rpc)"
info H-04 "bearer on MCP requests: $(fl mcp-auth)"
verdict H-04 yes "$(fl mcp-all-ok)" "every MCP request, GET and DELETE included, carries the bearer"
tool_result=$(fl tool-results | head -8)
verdict H-04b yes "$(has "$tool_result" "principal=contractor-test")" "the MCP tool answered through the model's tool call"
verdict H-04c yes "$(has "$tool_result" "untrusted_tool_result")" "Hermes wraps an MCP tool result in an untrusted-data envelope"
info H-04 "tools sent to the model ($(fl tool-count)); MCP ones: $(fl tool-names | tr ',' '\n' | grep '^mcp' | tr '\n' ' ')"
fake_start mcp
variant zs01-home-a no-tool-search-override.yaml -- -z "Who am I?"
info H-04 "with Hermes' default tool-search setting: $(fl tool-count) tools, $(fl tool-names | tr ',' '\n' | grep -c '^mcp') MCP names direct, bridge tools tool_search/tool_describe/tool_call: $(fl tool-names | tr ',' '\n' | grep -c '^tool_')"

# ------------------------------------------------------------------------------------------
step "H-05 where execute_code and the terminal tool run (ATK-02)"
vol zs01-home-b
fake_start tools
turn zs01-home-b --hostname zs01-turn -- -z "Run the tools"
results=$(fl tool-results)
echo "$results" | sed -n 1,3p | cut -c1-300 | sed 's/^/        /'
verdict H-05 yes "$(has "$results" "EXEC uid 10000" "host zs01-turn")" "execute_code runs in the instance container, as the instance user"
verdict H-05b yes "$(has "$results" "EXEC write /etc/hermes/config.yaml REFUSED" "EXEC write /opt/hermes/hermes_cli/main.py REFUSED" "EXEC write /usr/bin/zs-test REFUSED")" "code run by execute_code cannot write the policy, the install or /usr"
verdict H-05c yes "$(has "$results" "EXEC write /work/zs-test.txt OK" "EXEC write /opt/data/zs-test.txt OK")" "it can write the two volumes (control)"
verdict H-05d yes "$(has "$results" "CapEff:\\t0000000000000000" "NoNewPrivs:\\t1")" "the terminal tool runs with no capability and no-new-privileges"
verdict H-05e yes "$(has "$results" "EXEC docker.sock False")" "no runtime socket is visible to code run by the agent"
info H-05 "the write_file tool refuses /etc/hermes/config.yaml on its own: $(printf '%s' "$results" | grep -c 'Refusing to write to sensitive system path')"

# ------------------------------------------------------------------------------------------
step "H-06 the instance rewrites its own configuration (ATK-07)"
vol zs01-home-c
before=$(docker ps -aq | wc -l | tr -d ' ')
fake_start terminal
variant zs01-home-c docker-backend.yaml -- -z "Run echo"
after=$(docker ps -aq | wc -l | tr -d ' ')
info H-06 "terminal backend set to docker by the instance: $(fl tool-results | head -2 | tr '\n' ' ' | cut -c1-260)"
verdict H-06d yes "$(has "$(fl tool-results)" "Tool 'terminal' does not exist")" "with the docker backend and no socket, the terminal tool is not offered at all"
verdict H-06 "$before" "$after" "the docker backend creates no container: there is no socket to create one with"
turn zs01-home-c -- -z "Run echo"
cfg_after=$(docker run --rm -v zs01-home-c:/opt/data:ro --entrypoint cat "$IMG" /opt/data/config.yaml 2>&1)
verdict H-06b 0 "$(printf '%s' "$cfg_after" | grep -c 'backend: docker')" "a restart of the container puts the seed configuration back"
managed_check=$(docker run --rm "${HARDEN[@]}" --network zs01-net -v zs01-home-c:/opt/data "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" \
  --entrypoint sh "$IMG" -c "printf 'model:\n  provider: custom\n  default: evil\n  base_url: http://evil.example/v1\n  api_key: stolen\n' > /opt/data/config.yaml; $HERMES config 2>&1 | grep -i 'Model:' | head -2")
info H-06 "user config tries to repoint the model endpoint to evil.example; Hermes reports base_url $(printf '%s' "$managed_check" | grep -o "base_url': '[^']*'" | head -1)"
verdict H-06c yes "$(has "$managed_check" "fake-hub")" "the managed-scope policy keeps the model endpoint, the user config cannot override it"
fake_start mcp
variant zs01-home-c model-only.yaml -v "$SPK/configs/managed-mcp":/etc/hermes:ro -- -z "Who am I?"
verdict H-06e yes "$([ "$(fl mcp-auth)" = none ] && [ "$(fl count chat)" -ge 1 ] && echo yes || echo no)" "an MCP server named only in the managed file is not connected (the turn itself ran), so the managed scope does not carry it"

# ------------------------------------------------------------------------------------------
step "H-07 streaming"
fake_start basic
turn zs01-home-a -- -z "hi"
info H-07 "default: streams=$(fl streams), usage requested with stream_options=$(fl stream-options)"
fake_start basic
variant zs01-home-a stream-off.yaml -- -z "hi"
verdict H-07 false "$(fl streams)" "with model.streaming false the request is not streamed"
verdict H-07b "OK from the fake model." "$(printf '%s' "$TURN_OUT" | tail -1)" "the turn works without streaming"

# ------------------------------------------------------------------------------------------
step "H-08 the token: a changed value, and copies on disk"
vol zs01-home-r
fake_start basic
TOKEN=$TOKEN_A turn zs01-home-r -- -z "run one"
a_summary=$(fl auth-summary)
fake_start basic
TOKEN=$TOKEN_B turn zs01-home-r -- -z "run two"
b_summary=$(fl auth-summary)
info H-08 "first start with token A: $a_summary"
info H-08 "restart with token B on the same home volume: $b_summary"
b_new=$(printf '%s' "$b_summary" | sed 's/.*new=\([0-9]*\).*/\1/')
b_old=$(printf '%s' "$b_summary" | sed 's/ok=\([0-9]*\).*/\1/')
verdict H-08 yes "$([ "$b_old" = 0 ] && [ "$b_new" -ge 1 ] && echo yes || echo no)" "after a restart the instance uses the new token only (nothing keeps the old one)"
found=$(docker run --rm -v zs01-home-r:/v1:ro -v zs01-home-a:/v2:ro -v zs01-home-b:/v3:ro -v zs01-work:/v4:ro --entrypoint sh "$IMG" \
  -c "grep -rlaF -e $TOKEN_A -e $TOKEN_B /v1 /v2 /v3 /v4 2>/dev/null | head -5")
verdict H-08b "" "$found" "no copy of the token in the home volumes, the work volume or their logs and databases"
info H-08 "the token is in the environment of every process of the instance, tool subprocesses included (measured in H-05)"

# ------------------------------------------------------------------------------------------
step "H-09 one container per turn, with the conversation kept on the home volume"
vol zs01-home-s
fake_start two
turn zs01-home-s -- -z "first question"
first_ms=$TURN_MS
session=$(docker run --rm "${HARDEN[@]}" --network none -v zs01-home-s:/opt/data --entrypoint "$HERMES" "$IMG" sessions list 2>&1 | grep -Eo '[0-9]{8}_[0-9]{6}_[0-9a-f]+' | head -1)
turn zs01-home-s -- -z "second question" --resume "$session"
second_ms=$TURN_MS
info H-09 "container start to exit: first turn ${first_ms} ms, resumed turn ${second_ms} ms"
info H-09 "messages sent to the model, per turn: $(fl messages)"
verdict H-09 yes "$(fl messages | python3 -c 'import sys; n=[int(x) for x in sys.stdin.read().strip().split(",")]; print("yes" if len(n)==2 and n[1]>n[0] else "no")')" "a new container with --resume carries the earlier conversation"
turn zs01-home-s -- -z "third question" --continue
info H-09 "the same with --continue, no session id needed: messages sent per turn now $(fl messages)"

# ------------------------------------------------------------------------------------------
step "H-10 processes, listeners and memory during a turn; the cost of a turn"
vol zs01-home-m
fake_start basic FAKE_DELAY=8
docker run -d --name zs01-live "${HARDEN[@]}" --network zs01-net -v zs01-home-m:/opt/data -v zs01-work:/work -v "$SPK":/spike:ro \
  "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" "$IMG" -z "hold" >/dev/null
wait_for_chat 1
probe_json=$(docker exec zs01-live python3 /spike/probe.py)
echo "$probe_json" | python3 probe_fmt.py processes | sed 's/^/        /'
info H-10 "during a turn: $(echo "$probe_json" | python3 probe_fmt.py summary)"
verdict H-10 none "$(echo "$probe_json" | python3 probe_fmt.py listeners)" "no listening socket during a turn (TCP, UDP or Unix)"
docker wait zs01-live >/dev/null
docker rm -f zs01-live >/dev/null
fake_start basic
peaks=""
for i in 1 2 3; do
  t0=$(now_ms)
  out=$(docker run --rm "${HARDEN[@]}" --network zs01-net -v zs01-home-m:/opt/data -v zs01-work:/work "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" \
    --entrypoint sh "$IMG" -c "/opt/zephyros/entrypoint.sh -z hi >/dev/null 2>&1; echo \$(cat /sys/fs/cgroup/memory.peak) \$(cat /sys/fs/cgroup/pids.peak)" 2>&1)
  peaks="$peaks [${out}, $(($(now_ms) - t0)) ms]"
done
info H-10 "three turns, each [peak memory in bytes, peak processes and threads, wall time]:$peaks"

# ------------------------------------------------------------------------------------------
step "H-11 the gateway with the webhook platform, the only inbound path Hermes offers"
vol zs01-home-g
fake_start basic
docker run -d --name zs01-gw "${HARDEN[@]}" --network zs01-net -v zs01-home-g:/opt/data -v zs01-work:/work -v "$SPK":/spike:ro \
  "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" --entrypoint sh "$IMG" -c "cp /spike/configs/webhook.yaml /opt/data/config.yaml && exec $HERMES gateway run" >/dev/null
up=no
for i in $(seq 1 60); do
  if docker exec zs01-gw python3 /spike/probe.py 2>/dev/null | python3 probe_fmt.py tcp-listeners | grep -q 8644; then up=yes; break; fi
  sleep 1
done
verdict H-11 yes "$up" "the gateway opens the webhook port"
sleep 5
idle_json=$(docker exec zs01-gw python3 /spike/probe.py)
info H-11 "idle gateway: $(echo "$idle_json" | python3 probe_fmt.py summary)"
info H-11 "idle gateway listeners: $(echo "$idle_json" | python3 probe_fmt.py listeners)"
echo "$idle_json" | python3 probe_fmt.py processes | sed 's/^/        /'
posted=$(docker run -i --rm --network zs01-net "$PY" python - < post_webhook.py)
verdict H-11b "401 202" "$posted" "an unsigned event is refused, a signed one is accepted"
wait_for_chat 1
verdict H-11c yes "$([ "$(fl count chat)" -ge 1 ] && echo yes || echo no)" "the accepted event started an agent turn that called the model"
sleep 2
info H-11 "after one event: $(docker exec zs01-gw python3 /spike/probe.py | python3 probe_fmt.py summary)"
info H-11 "background work in the gateway: $(docker exec zs01-gw sh -c 'grep -hoE "(Cron scheduler started|In-process cron scheduler started|kanban dispatcher: embedded in gateway|Gateway housekeeping started)" /opt/data/logs/gateway.log | sort -u | tr "\n" ";"')"
docker rm -f zs01-gw >/dev/null

# ------------------------------------------------------------------------------------------
step "H-12 plugins.isolation"
for mode in in_process host; do
  vol "zs01-home-p-$mode"
  fake_start plugin
  docker run --rm "${HARDEN[@]}" --network zs01-net -v "zs01-home-p-$mode":/opt/data -v "$SPK":/spike:ro "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" \
    --entrypoint sh "$IMG" -c "cp /opt/zephyros/seed.yaml /opt/data/config.yaml && printf 'plugins:\n  enabled: [zs_probe]\n  isolation: $mode\n' >> /opt/data/config.yaml && mkdir -p /opt/data/plugins && cp -r /spike/plugin/zs_probe /opt/data/plugins/ && exec $HERMES -z 'probe the plugin'" >/dev/null 2>&1
  read -r plugin_pid hermes_pid host_procs <<< "$(fl plugin-check)"
  if [ "$mode" = in_process ]; then
    verdict H-12 "$hermes_pid" "$plugin_pid" "in_process: the plugin's code runs inside the Hermes process"
  else
    verdict H-12b yes "$([ "$plugin_pid" != "?" ] && [ "$plugin_pid" != "$hermes_pid" ] && [ "$host_procs" -ge 1 ] && echo yes || echo no)" "host: the plugin's code runs in a child process (plugin_host_child), same container, same user"
  fi
done

# ------------------------------------------------------------------------------------------
step "H-13 no Internet: what the instance asks for outside the hub"
docker network create --internal --label "$LABEL" zs01-int >/dev/null
vol zs01-home-n
fake_start basic
docker network connect zs01-int fake-hub
fake_ip=$(docker inspect fake-hub -f '{{(index .NetworkSettings.Networks "zs01-int").IPAddress}}')
docker run -d --name zs01-sniff --label "$LABEL" --network zs01-int --cap-drop ALL --cap-add NET_RAW --cap-add NET_ADMIN \
  --entrypoint sh "$SNIFFER" -c "tcpdump -nn -l -i lo -w /tmp/dns.pcap & tcpdump -nn -l -i eth0 -w /tmp/eth.pcap 'not arp and not icmp6 and not ip6 multicast and not host $fake_ip' & wait; exec sleep 600" >/dev/null
sleep 2
docker run --rm "${HARDEN[@]}" --network container:zs01-sniff -v zs01-home-n:/opt/data -v zs01-work:/work \
  "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" "$IMG" -z "hi" >/dev/null 2>&1
sleep 1
docker exec zs01-sniff pkill -INT tcpdump
sleep 1
mkdir -p "$CAPDIR"
docker cp zs01-sniff:/tmp/dns.pcap "$CAPDIR/dns.pcap" >/dev/null
names=$(python3 dns_names.py "$CAPDIR/dns.pcap" | sort -u | tr '\n' ',' | sed 's/,$//')
leaks=$(docker exec zs01-sniff sh -c 'tcpdump -nn -r /tmp/eth.pcap 2>/dev/null | wc -l' | tr -d ' ')
info H-13 "DNS questions asked during the turn: ${names:-none}"
verdict H-13 yes "$(has "$names" "fake-hub")" "control: the capture saw the instance look up the hub's name"
verdict H-13b "fake-hub A,fake-hub AAAA" "$names" "the instance looked up no name but the hub's"
verdict H-13c 0 "$leaks" "no IP packet reached the link toward an address other than the hub (a connect with no route makes no packet, so this check is weak; the guard tests of Z2-08 are the real one)"
verdict H-13d yes "$([ "$(fl count chat)" -ge 1 ] && echo yes || echo no)" "the turn completed on a network with no route to the Internet"
docker rm -f zs01-sniff >/dev/null

# ------------------------------------------------------------------------------------------
step "H-14 when the hub does not answer"
vol zs01-home-f
fake_start basic FAKE_MCP_DOWN=1
turn zs01-home-f -- -z "hi"
info H-14 "MCP half down, model up: output '$(printf '%s' "$TURN_OUT" | tail -1)' after ${TURN_MS} ms (the turn goes on without hub tools)"
docker rm -f fake-hub >/dev/null
docker run -d --name zs01-down "${HARDEN[@]}" --network zs01-net -v zs01-home-f:/opt/data -v zs01-work:/work \
  "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" "$IMG" -z "hi" >/dev/null
sleep 45
running=$(docker inspect zs01-down --format '{{.State.Running}}')
info H-14 "hub absent: the instance is still running after 45 s: $running (measured once in exploration: Hermes gave up after 5 min 27 s)"
docker rm -f zs01-down >/dev/null

# ------------------------------------------------------------------------------------------
step "H-15 changing the engine from inside"
vol zs01-home-u
upd=$(docker run --rm "${HARDEN[@]}" --network none -v zs01-home-u:/opt/data -v zs01-work:/work "$IMG" update 2>&1 | head -4 | tr '\n' '|' | cut -c1-300)
info H-15 "hermes update inside a hardened instance: $upd"
pipout=$(docker run --rm "${HARDEN[@]}" --network none -v zs01-home-u:/opt/data -v zs01-work:/work --entrypoint /opt/hermes/.venv/bin/python "$IMG" -m pip install --no-input zs-nonexistent 2>&1 | tail -1 | cut -c1-200)
info H-15 "pip into the pinned environment: $pipout"
fresh=$(docker run --rm --network none --entrypoint "$HERMES" "$IMG" --version 2>&1 | head -1)
verdict H-15 yes "$(has "$fresh" "upstream ${REVISION:0:8}")" "a new container starts with the pinned version, whatever an earlier one did"

# ------------------------------------------------------------------------------------------
step "H-16 the heaviest turn of the spike: execute_code, terminal, write_file and an MCP tool"
vol zs01-home-h
fake_start tools
heavy=$(docker run --rm "${HARDEN[@]}" --network zs01-net -v zs01-home-h:/opt/data -v zs01-work:/work "${ENVS[@]}" -e "ZEPHYROS_TOKEN=$TOKEN_A" \
  --entrypoint sh "$IMG" -c "/opt/zephyros/entrypoint.sh -z go >/dev/null 2>&1; echo \$(cat /sys/fs/cgroup/memory.peak) \$(cat /sys/fs/cgroup/pids.peak)" 2>&1)
info H-16 "peak memory (bytes) and peak processes plus threads for that turn: $heavy"

# ------------------------------------------------------------------------------------------
step "H-17 does a configured context length stop the probes of the model endpoint? (decision 4)"
fake_start basic
variant zs01-home-a context-length.yaml -- -z "hi"
info H-17 "with model.context_length set, requests by path:"
fl paths | sed 's/^/        /'
info H-17 "requests without a bearer: $(fl missing-paths)"

printf '\n== summary: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
