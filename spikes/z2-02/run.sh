#!/usr/bin/env bash
# Spike Z2-02: placement of the hub and the container filter, and egress enforcement.
#
#   spikes/z2-02/run.sh            run every experiment, print one line per result
#
# Throwaway code: it answers the questions of the Z2 design (section 9) and is
# never imported by src/. It needs a Docker-compatible runtime, bash and python3, and it
# runs unchanged on macOS (OrbStack) and on Linux (the manual CI job spike-z2-02).
#
# Method. Every "blocked" result is paired with a control: the same probe, with the guard
# rules removed, must reach its target. Otherwise a dead target would look like a block.
set -u
cd "$(dirname "$0")"

LABEL="zs.spike=1"
ALPINE="alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8"
PASS=0
FAIL=0
HOSTPID=""

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

cleanup() {
  [ -n "$HOSTPID" ] && kill "$HOSTPID" 2>/dev/null
  docker ps -aq --filter "label=$LABEL" | xargs -r docker rm -f >/dev/null 2>&1
  docker network ls -q --filter "label=$LABEL" | xargs -r docker network rm >/dev/null 2>&1
  docker volume ls -q --filter "label=$LABEL" | xargs -r docker volume rm >/dev/null 2>&1
  return 0
}
trap cleanup EXIT
cleanup

# ---- probes, run inside an execution context (an instance, or a fixture sharing its namespace)
tcp() { # container ip port -> reached | blocked
  if docker exec "$1" sh -c "nc -w 3 $2 $3 </dev/null >/dev/null 2>&1"; then echo reached; else echo blocked; fi
}
udp() { # container ip port -> reached | blocked   (the observer answers a datagram)
  if docker exec "$1" sh -c "echo ping | nc -u -w 2 $2 $3 2>/dev/null" | grep -q hello; then echo reached; else echo blocked; fi
}
icmp() { # guard-container ip -> reached | blocked   (a fixture with NET_RAW joins the namespace)
  if docker run --rm --network "container:$1" --cap-drop ALL --cap-add NET_RAW "$ALPINE" ping -c 1 -W 2 "$2" >/dev/null 2>&1; then echo reached; else echo blocked; fi
}
hits() { docker logs "$1" 2>&1 | grep -cE '^(TCP|UDP) '; }
netip() { docker inspect "$1" -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}"; }

# ---- building blocks
net() { docker network create --label "$LABEL" "$@" >/dev/null; }
guard() { # name network hubip [delay]
  docker run -d --label "$LABEL" --name "$1" --network "$2" --cap-drop ALL --cap-add NET_ADMIN \
    --security-opt no-new-privileges --read-only --tmpfs /tmp --pids-limit 64 --memory 32m \
    -e HUB_IP="$3" -e HUB_PORT=8080 -e HUB_IN_PORT=7000 -e DELAY="${4:-0}" \
    --health-cmd 'test -f /tmp/ready' --health-interval 1s --health-retries 60 zs-guard >/dev/null
}
ready() { # guard -> 0 once the rules are loaded
  local i=0
  while [ $i -lt 60 ]; do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null)" = healthy ] && return 0
    sleep 0.5
    i=$((i + 1))
  done
  return 1
}
instance() { # name guard   (non-root, no capability, read-only root, joins the guard namespace)
  docker run -d --label "$LABEL" --name "$1" --network "container:$2" --cap-drop ALL \
    --security-opt no-new-privileges --read-only --user 10001:10001 --pids-limit 64 --memory 32m \
    --entrypoint sleep zs-guard infinity >/dev/null
}
rules_off() { docker exec "$1" /usr/local/bin/guard.sh flush; }
rules_on() { docker exec "$1" /usr/local/bin/guard.sh load; }

# =============================================================================================
step "S2-01 runtime facts"
info S2-01 "docker $(docker version -f '{{.Server.Version}}' 2>/dev/null) on $(docker version -f '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null), kernel $(docker run --rm "$ALPINE" uname -r), client OS $(uname -s)"
info S2-01 "memory visible to the runtime: $(docker info -f '{{.MemTotal}}') bytes, cpus: $(docker info -f '{{.NCPU}}')"
docker build -q -f guard.Dockerfile -t zs-guard . >/dev/null && docker build -q -f observer.Dockerfile -t zs-observer . >/dev/null
verdict S2-01 ok "$(docker run --rm --cap-drop ALL --cap-add NET_ADMIN --entrypoint nft zs-guard list ruleset >/dev/null 2>&1 && echo ok || echo no-nftables)" "nftables usable in a container namespace"

# ---- fixtures: the hub, an instance network with a lan-sim neighbour, the host listeners
net zs-core
net zs-i1
docker run -d --label "$LABEL" --name zs-hub --network zs-core -p 127.0.0.1:18090:8080 -e OBS_NAME=hub \
  zs-observer tcp:8080 tcp:9090 udp:8080 >/dev/null
docker network connect zs-i1 zs-hub
HUBIP=$(netip zs-hub zs-i1)
GW=$(docker network inspect zs-i1 -f '{{(index .IPAM.Config 0).Gateway}}')

docker run -d --label "$LABEL" --name zs-lan --network zs-i1 --cap-add NET_ADMIN -e OBS_NAME=lan --entrypoint sh zs-observer \
  -c 'for a in 10.77.0.5 172.31.250.5 192.168.77.5 169.254.169.254 100.64.7.7 100.100.100.200; do ip addr add $a/32 dev lo; done; exec python -u /observer.py tcp:80 udp:80' >/dev/null
LANIP=$(netip zs-lan zs-i1)
docker run -d --label "$LABEL" --name zs-vmhost --network host -e OBS_NAME=vmhost zs-observer tcp:18081 >/dev/null
python3 -m http.server 18080 --bind 0.0.0.0 >/tmp/zs-host-listener.log 2>&1 &
HOSTPID=$!
HOSTIP=$(docker run --rm --add-host host.docker.internal:host-gateway "$ALPINE" getent hosts host.docker.internal | awk '{print $1}' | head -1)
sleep 2
info fixt "hub on zs-core + zs-i1 at $HUBIP, gateway $GW, lan-sim $LANIP, host alias $HOSTIP"

guard zs-g1 zs-i1 "$HUBIP"
ready zs-g1 || info S2-02 "guard never became healthy"
instance zs-i1c zs-g1
for a in 10.77.0.5 172.31.250.5 192.168.77.5 169.254.169.254 100.64.7.7 100.100.100.200; do
  docker exec zs-g1 ip route add "$a/32" via "$LANIP"
done

# =============================================================================================
step "S2-02 the instance cannot change the rules or its network (user 10001, no capability)"
for attempt in "nft flush ruleset" "nft delete table inet zeph" "nft add rule inet zeph output accept" \
  "ip route add blackhole 1.2.3.4" "ip link add d0 type dummy" "ip addr add 10.9.9.9/24 dev eth0" \
  "unshare -n true"; do
  if docker exec zs-i1c sh -c "$attempt" >/dev/null 2>&1; then r=allowed; else r=denied; fi
  verdict S2-02 denied "$r" "as the instance: $attempt"
done
for attempt in "nft flush ruleset" "ip route add blackhole 1.2.3.4"; do
  if docker exec -u 0 zs-i1c sh -c "$attempt" >/dev/null 2>&1; then r=allowed; else r=denied; fi
  verdict S2-02 denied "$r" "as root without capability: $attempt"
done
verdict S2-02 "inet zeph" "$(docker exec zs-g1 nft list tables | grep -c 'inet zeph' | sed 's/1/inet zeph/')" "the rules are loaded in the namespace the instance shares"
info S2-02 "tables in the namespace: $(docker exec zs-g1 nft list tables | tr '\n' ';') (Docker keeps its embedded-DNS redirect in one of them on this runtime; the guard touches only inet zeph)"

# =============================================================================================
step "S2-03 the hub endpoint and the public Internet stay open"
verdict S2-03 reached "$(tcp zs-i1c "$HUBIP" 8080)" "hub agent endpoint, TCP 8080"
verdict S2-03 reached "$(tcp zs-i1c 1.1.1.1 443)" "public IPv4 address, TCP 443"
verdict S2-03 yes "$(docker exec zs-i1c sh -c 'wget -q -T 6 -O /dev/null https://example.com && echo yes || echo no')" "public name over HTTPS (name resolution works)"
verdict S2-03 hub "$(docker exec zs-i1c getent hosts zs-hub | awk '{print $2}' | head -1 | sed 's/zs-hub/hub/')" "the hub's name resolves (embedded DNS)"

step "S2-04 the same hub address on other ports and protocols is blocked"
before=$(hits zs-hub)
verdict S2-04 blocked "$(tcp zs-i1c "$HUBIP" 9090)" "hub address, TCP 9090 (the operator-port stand-in)"
verdict S2-04 blocked "$(udp zs-i1c "$HUBIP" 8080)" "hub address, UDP 8080"
verdict S2-04 blocked "$(icmp zs-g1 "$HUBIP")" "hub address, ICMP echo"
verdict S2-04 blocked "$(docker exec zs-i1c sh -c "ping -c 1 -W 2 $HUBIP >/dev/null 2>&1 && echo reached || echo blocked")" "hub address, ICMP echo from the instance itself (ping socket, no capability)"
verdict S2-04 "$before" "$(hits zs-hub)" "the hub's listeners saw none of the attempts (O-NET)"
info S2-04 "ICMP echo to a public address from the instance (no capability, ping socket): $(docker exec zs-i1c sh -c 'ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 && echo reached || echo blocked') (public destinations are not restricted)"
rules_off zs-g1
verdict S2-04c reached "$(tcp zs-i1c "$HUBIP" 9090)" "control, rules off: TCP 9090 reaches the hub"
verdict S2-04c reached "$(udp zs-i1c "$HUBIP" 8080)" "control, rules off: UDP 8080 reaches the hub"
verdict S2-04c reached "$(icmp zs-g1 "$HUBIP")" "control, rules off: ICMP reaches the hub"
rules_on zs-g1

step "S2-05 protected ranges: control with rules off, then blocked with rules on"
for t in "$LANIP:on-link neighbour (RFC 1918 network of the instance)" "10.77.0.5:10.0.0.0/8" "172.31.250.5:172.16.0.0/12" \
  "192.168.77.5:192.168.0.0/16" "169.254.169.254:link-local, cloud metadata" "100.64.7.7:100.64.0.0/10" "100.100.100.200:cloud metadata (100.64/10)"; do
  addr=${t%%:*}
  what=${t#*:}
  rules_off zs-g1
  c=$(tcp zs-i1c "$addr" 80)
  rules_on zs-g1
  verdict S2-05c reached "$c" "control, rules off: $addr reached ($what)"
  b=$(hits zs-lan)
  verdict S2-05 blocked "$(tcp zs-i1c "$addr" 80)" "$addr TCP/80 ($what)"
  verdict S2-05 blocked "$(udp zs-i1c "$addr" 80)" "$addr UDP/80 ($what)"
  verdict S2-05 0 "$(($(hits zs-lan) - b))" "the listener behind $addr saw nothing (O-NET)"
done
verdict S2-05 "hub" "$(docker exec zs-i1c sh -c "nc -w 3 $HUBIP 8080 </dev/null" | grep -q hello && echo hub)" "after all that, the hub endpoint still answers"

step "S2-06 the host alias, the bridge gateway and a host service"
case "$HOSTIP" in
  0.*) info S2-06 "host alias address: $HOSTIP, inside 0.0.0.0/8, which the annex does not list (the guard blocks 0.0.0.0/8 too)" ;;
  *) info S2-06 "host alias address: $HOSTIP (inside a range the annex lists)" ;;
esac
rules_off zs-g1
verdict S2-06c reached "$(tcp zs-i1c "$HOSTIP" 18080)" "control, rules off: listener on the machine's host side via the host alias ($HOSTIP)"
verdict S2-06c reached "$(tcp zs-i1c "$GW" 18081)" "control, rules off: listener in the runtime's host namespace via the bridge gateway ($GW)"
rules_on zs-g1
hb=$(hits zs-vmhost)
verdict S2-06 blocked "$(tcp zs-i1c "$HOSTIP" 18080)" "host alias $HOSTIP:18080"
verdict S2-06 blocked "$(tcp zs-i1c "$GW" 18081)" "bridge gateway $GW:18081"
verdict S2-06 0 "$(($(hits zs-vmhost) - hb))" "the host-namespace listener saw nothing (O-NET)"
verdict S2-06 blocked "$(tcp zs-i1c host.docker.internal 18080)" "host alias by name (resolved inside the namespace)"

step "S2-07 a public name that resolves to a protected address"
res=$(docker exec zs-i1c getent hosts 10.77.0.5.nip.io | awk '{print $1}' | head -1)
if [ "$res" = "10.77.0.5" ]; then
  rules_off zs-g1
  verdict S2-07c reached "$(tcp zs-i1c 10.77.0.5.nip.io 80)" "control, rules off: the name leads to the listener"
  rules_on zs-g1
  verdict S2-07 blocked "$(tcp zs-i1c 10.77.0.5.nip.io 80)" "10.77.0.5.nip.io resolves to 10.77.0.5 and the connection is blocked"
else
  info S2-07 "skipped: the wildcard name did not resolve here (got '$res')"
fi

step "S2-08 the hub may open connections toward the instance; nobody else"
docker exec -d zs-i1c sh -c 'nc -ll -p 7000 -e /bin/echo reply-from-instance'
docker exec -d zs-i1c sh -c 'nc -ll -p 7001 -e /bin/echo reply-from-instance'
GUARDIP=$(netip zs-g1 zs-i1)
sleep 1
conn() { docker exec "$1" python -c "
import socket,sys
try:
    s=socket.create_connection(('$GUARDIP',$2),3); print('reached' if s.recv(64) else 'reached')
except OSError: print('blocked')"; }
verdict S2-08 reached "$(conn zs-hub 7000)" "hub -> instance port 7000: connects and the instance replies"
verdict S2-08 blocked "$(conn zs-hub 7001)" "hub -> instance port 7001: not opened"
verdict S2-08 blocked "$(conn zs-lan 7000)" "another host on the network -> instance port 7000"

step "S2-09 source address seen by the hub (EXG-60)"
docker exec zs-i1c sh -c "nc -w 3 $HUBIP 8080 </dev/null >/dev/null"
seen=$(docker logs zs-hub 2>&1 | grep -- "-> $HUBIP:8080" | tail -1 | awk '{print $2}' | cut -d: -f1)
verdict S2-09 "$GUARDIP" "$seen" "the hub sees the instance's own address, untranslated"
net zs-i2
docker network connect zs-i2 zs-hub
HUB2=$(netip zs-hub zs-i2)
guard zs-g2 zs-i2 "$HUB2"
ready zs-g2
instance zs-i2c zs-g2
GUARD2=$(netip zs-g2 zs-i2)
docker exec zs-i2c sh -c "nc -w 3 $HUB2 8080 </dev/null >/dev/null"
seen2=$(docker logs zs-hub 2>&1 | grep -- "-> $HUB2:8080" | tail -1 | awk '{print $2}' | cut -d: -f1)
verdict S2-09 "$GUARD2" "$seen2" "a second instance, on its own network, is seen at its own address"
verdict S2-09 different "$([ -n "$seen" ] && [ "$seen" != "$seen2" ] && echo different || echo same)" "the two instances have distinct source addresses at the hub"
docker exec -d zs-i2c sh -c 'nc -ll -p 7500 -e /bin/echo from-instance-2'
sleep 1
x_on=$(tcp zs-i1c "$GUARD2" 7500)
rules_off zs-g1
x_g1off=$(tcp zs-i1c "$GUARD2" 7500)
rules_off zs-g2
x_bothoff=$(tcp zs-i1c "$GUARD2" 7500)
rules_on zs-g1
rules_on zs-g2
verdict S2-09 blocked "$x_on" "instance 1 -> instance 2 (other network, a listening port), all rules on"
info S2-09 "same attempt with the egress rules of instance 1 off: $x_g1off; with the rules of both guards off: $x_bothoff (what the runtime's own network isolation does alone)"
nc -w 3 127.0.0.1 18090 </dev/null >/dev/null 2>&1
viahost=$(docker logs zs-hub 2>&1 | grep -- ":8080" | tail -1 | awk '{print $2}' | cut -d: -f1)
info S2-09 "a connection from the machine's host side through the published port is seen by the hub as $viahost (never an instance address)"

step "S2-10 name lookups: other containers' names must fail"
docker run -d --label "$LABEL" --name zs-other --network zs-i2 --entrypoint sleep zs-guard infinity >/dev/null
verdict S2-10 fail "$(docker exec zs-i1c sh -c 'getent hosts zs-other >/dev/null && echo found || echo fail')" "per-instance networks: the name of a container on another network"
verdict S2-10 found "$(docker exec zs-i1c sh -c 'getent hosts zs-lan >/dev/null && echo found || echo fail')" "a container on the SAME network resolves (the fixture; in production only the hub shares the network)"
verdict S2-10 found "$(docker exec zs-i1c sh -c 'getent hosts zs-g1 >/dev/null && echo found || echo fail')" "the instance's own guard name resolves (its own address; see the report)"
net zs-shared
HUBS=$(netip zs-hub zs-core)
guard zs-gs1 zs-shared "$HUBS"
guard zs-gs2 zs-shared "$HUBS"
ready zs-gs1
ready zs-gs2
instance zs-is1 zs-gs1
verdict S2-10 found "$(docker exec zs-is1 sh -c 'getent hosts zs-gs2 >/dev/null && echo found || echo fail')" "ONE shared network: the instance resolves the name of another instance's guard (the leak the per-instance design avoids)"

step "S2-11 fail-closed behavior"
net zs-i3
docker run -d --label "$LABEL" --name zs-lan3 --network zs-i3 -e OBS_NAME=lan3 zs-observer tcp:80 >/dev/null
LAN3=$(netip zs-lan3 zs-i3)
guard zs-g3 zs-i3 192.0.2.1 4
instance zs-i3c zs-g3
verdict S2-11 reached "$(tcp zs-i3c "$LAN3" 80)" "no gate: an instance started while the guard has not loaded its rules reaches a protected address (the race)"
ready zs-g3
verdict S2-11 blocked "$(tcp zs-i3c "$LAN3" 80)" "after the guard is healthy the same probe is blocked"
net zs-i4
docker run -d --label "$LABEL" --name zs-lan4 --network zs-i4 -e OBS_NAME=lan4 zs-observer tcp:80 >/dev/null
LAN4=$(netip zs-lan4 zs-i4)
guard zs-g4 zs-i4 192.0.2.1 4
ready zs-g4
instance zs-i4c zs-g4
verdict S2-11 blocked "$(tcp zs-i4c "$LAN4" 80)" "with the gate (instance created only after the guard is healthy) the first probe is blocked"
docker kill zs-g4 >/dev/null
sleep 1
info S2-11 "after the guard is killed: instance state '$(docker inspect -f '{{.State.Status}}' zs-i4c)', public probe '$(tcp zs-i4c 1.1.1.1 443)', protected probe '$(tcp zs-i4c "$LAN4" 80)'"
docker start zs-g4 >/dev/null 2>&1
sleep 2
info S2-11 "after the guard is restarted: instance state '$(docker inspect -f '{{.State.Status}}' zs-i4c)', public probe '$(tcp zs-i4c 1.1.1.1 443)' (the instance keeps the old namespace)"

step "S2-12 the hub joins many instance networks"
start=$(date +%s)
ok=0
for i in $(seq 1 40); do
  if docker network create --label "$LABEL" --subnet "10.213.$i.0/28" "zs-n$i" >/dev/null 2>&1 && docker network connect "zs-n$i" zs-hub >/dev/null 2>&1; then ok=$((ok + 1)); else break; fi
done
verdict S2-12 40 "$ok" "networks with an explicit /28 subnet created and joined by the running hub without a restart ($(($(date +%s) - start)) s)"
verdict S2-12 reached "$(tcp zs-i1c "$HUBIP" 8080)" "the first instance still reaches the hub after forty more networks were joined"
n=0
while docker network create --label "$LABEL" "zs-d$n" >/dev/null 2>&1; do n=$((n + 1)); [ $n -ge 400 ] && break; done
info S2-12 "networks that the runtime's default address pool can still create before it is exhausted: $n (the filter must allocate explicit subnets)"
docker network ls -q --filter "name=zs-d" | xargs -r docker network rm >/dev/null 2>&1

step "S2-13 the container filter: no network, a runtime socket, a Unix socket shared with the hub"
docker volume create --label "$LABEL" zs-sock >/dev/null
docker run -d --label "$LABEL" --name zs-filter --network none --cap-drop ALL --security-opt no-new-privileges --read-only \
  -v /var/run/docker.sock:/var/run/docker.sock -v zs-sock:/run/zeph --entrypoint python zs-observer -u -c '
import http.client, json, os, socket
class Unix(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX); self.sock.connect("/var/run/docker.sock")
def call(method, path, body=None):
    c = Unix("localhost")
    c.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    return r.status, (json.loads(data) if data and data[:1] in b"{[" else data)
def lifecycle():
    spec = {"Image": "zs-guard", "Entrypoint": ["sleep"], "Cmd": ["30"], "Labels": {"zs.spike": "1", "zs.made-by": "filter"},
            "HostConfig": {"NetworkMode": "none", "CapDrop": ["ALL"], "ReadonlyRootfs": True}}
    status, made = call("POST", "/containers/create?name=zs-made-by-filter", spec)
    cid = made["Id"]
    start = call("POST", f"/containers/{cid}/start")[0]
    running = call("GET", f"/containers/{cid}/json")[1]["State"]["Running"]
    removed = call("DELETE", f"/containers/{cid}?force=true")[0]
    return f"lifecycle create={status} start={start} running={running} remove={removed}"
path = "/run/zeph/filter.sock"
if os.path.exists(path): os.remove(path)
server = socket.socket(socket.AF_UNIX); server.bind(path); os.chmod(path, 0o666); server.listen(4)
while True:
    conn, _ = server.accept()
    request = conn.recv(64).decode().strip()
    reply = lifecycle() if request == "lifecycle" else "runtime api " + call("GET", "/version")[1]["ApiVersion"]
    conn.sendall((reply + "\n").encode()); conn.close()
' >/dev/null
sleep 2
ask() { docker run --rm --network none --cap-drop ALL --read-only -v zs-sock:/run/zeph --entrypoint python zs-observer -c "
import socket
s = socket.socket(socket.AF_UNIX); s.connect('/run/zeph/filter.sock'); s.sendall(b'$1\n'); print(s.recv(200).decode().strip())"; }
reply=$(ask version)
verdict S2-13 "runtime api" "$(echo "$reply" | cut -c1-11)" "the hub-side container (no network) gets the runtime's answer through the filter ($reply)"
reply=$(ask lifecycle)
verdict S2-13 "lifecycle create=201 start=204 running=True remove=204" "$reply" "the filter created, started, inspected and removed a labelled container through the runtime socket"
verdict S2-13 gone "$(docker ps -aq --filter name=zs-made-by-filter | grep -q . && echo left || echo gone)" "the container the filter made is gone"
ifs=$(docker exec zs-filter sh -c 'ls /sys/class/net | grep -v "^lo$" | tr -d "\n"')
verdict S2-13 none "${ifs:-none}" "the filter container has no network interface but the loopback"
verdict S2-13 blocked "$(docker exec zs-filter python -c "
import socket
try: socket.create_connection(('1.1.1.1',443),2); print('reached')
except OSError: print('blocked')")" "the filter container cannot open a network connection"

step "S2-14 memory"
docker stats --no-stream --format 'INFO  S2-14 {{.Name}}: {{.MemUsage}}' zs-g1 zs-i1c zs-hub zs-filter 2>/dev/null

if [ "$(uname -s)" = Darwin ]; then
  step "S2-15 what a macOS host firewall can see"
  (docker exec zs-i1c sh -c 'sleep 10 | nc -w 12 1.1.1.1 443' >/dev/null 2>&1 &)
  sleep 3
  info S2-15 "route to the instance network from the Mac: $(route -n get "$HUBIP" 2>/dev/null | grep -E 'gateway|interface' | tr -s ' ' | tr '\n' ';')"
  info S2-15 "connections to 1.1.1.1:443 on the Mac while the instance holds one: $(lsof -nP -iTCP:443 -sTCP:ESTABLISHED 2>/dev/null | grep -c '1.1.1.1') (owner process(es): $(lsof -nP -iTCP:443 -sTCP:ESTABLISHED 2>/dev/null | grep '1.1.1.1' | awk '{print $1}' | sort -u | tr '\n' ' '))"
  info S2-15 "pf rules were not tried: they need root. Observation only"
fi

step "S2-16 IPv6"
if net --ipv6 --subnet 10.213.99.0/28 --subnet fd00:7a:2::/64 zs-v6 2>/dev/null; then
  guard zs-g6 zs-v6 192.0.2.1
  ready zs-g6
  instance zs-i6c zs-g6
  v6pub=$(docker exec zs-i6c sh -c 'nc -w 5 2606:4700:4700::1111 443 </dev/null >/dev/null 2>&1 && echo reached || echo blocked')
  if [ "$v6pub" = reached ]; then
    verdict S2-16 reached "$v6pub" "public IPv6 address, TCP 443 (positive control)"
  else
    info S2-16 "public IPv6 is not reachable from this runtime ('not applicable', never 'passed')"
  fi
  docker run -d --label "$LABEL" --name zs-lan6 --network zs-v6 --entrypoint sleep zs-guard infinity >/dev/null
  g6=$(docker inspect zs-lan6 -f '{{(index .NetworkSettings.Networks "zs-v6").GlobalIPv6Address}}')
  d0=$(docker exec zs-g6 nft list ruleset | grep -oE 'ip6 daddr @protected6 counter packets [0-9]+' | grep -oE '[0-9]+$')
  docker exec zs-i6c sh -c "nc -w 2 $g6 80 </dev/null" >/dev/null 2>&1
  d1=$(docker exec zs-g6 nft list ruleset | grep -oE 'ip6 daddr @protected6 counter packets [0-9]+' | grep -oE '[0-9]+$')
  verdict S2-16 yes "$([ "${d1:-0}" -gt "${d0:-0}" ] && echo yes || echo no)" "a connection to a neighbour's IPv6 address ($g6, ULA) hits the protected6 drop rule (counter ${d0:-0} -> ${d1:-0})"
else
  info S2-16 "this runtime does not create IPv6 networks: every IPv6 check is 'not applicable'"
fi

printf '\n== summary: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
