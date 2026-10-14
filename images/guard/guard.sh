#!/bin/sh
# The network guard of a Zephyros instance (annex "Network topology of instances").
#
# It owns the network namespace that the agent container of the instance joins, loads the rules
# of annex "Protected networks" into it, and then waits. It runs no listener and holds no secret.
# The agent container has no capability, so it cannot change these rules.
#
#   guard.sh run     load the rules, then wait (the entrypoint)
#   guard.sh check   exit 0 when the rules are in place (the health check of the container)
#   guard.sh load    load the rules again (used by the tests, which take them off for controls)
#
# Environment: HUB_IP and HUB_PORT, the agent endpoint (TCP), permitted at every level;
# EGRESS, the level of ADR 0024 (regulated, the default; sealed; open); PROXY_PORT, the egress
# proxy of the hub, permitted to a regulated instance only.
#
#   regulated  the hub's agent endpoint and its egress proxy; everything else is refused
#   sealed     the hub's agent endpoint; everything else is refused
#   open       every destination outside the protected networks (ADR 0009, superseded)
set -eu

fail() { echo "guard: $*" >&2; exit 1; }

# Values that go into the rules are checked first: the rules are text.
printf '%s' "${HUB_IP:-}" | grep -Eqx '([0-9]{1,3}\.){3}[0-9]{1,3}' || fail "HUB_IP is not an IPv4 address"
printf '%s' "${HUB_PORT:-}" | grep -Eqx '[0-9]{1,5}' || fail "HUB_PORT is not a port"
EGRESS="${EGRESS:-regulated}"
case "$EGRESS" in
  regulated)
    printf '%s' "${PROXY_PORT:-}" | grep -Eqx '[0-9]{1,5}' || fail "PROXY_PORT is not a port"
    [ "$PROXY_PORT" != "$HUB_PORT" ] || fail "PROXY_PORT is the port of the agent endpoint"
    proxy_rule="ip daddr ${HUB_IP} tcp dport ${PROXY_PORT} accept"
    last_rule="reject" ;;
  sealed) proxy_rule="" last_rule="reject" ;;
  open) proxy_rule="" last_rule="" ;;
  *) fail "EGRESS is not regulated, sealed or open" ;;
esac

load() {
  # Only the table of the guard is touched. Docker keeps the redirect of its embedded name
  # resolver in an "ip nat" table of this namespace; a "flush ruleset" would break it.
  nft -f - <<RULES
table inet zeph
delete table inet zeph
table inet zeph {
  set protected4 {
    type ipv4_addr
    flags interval
    elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16,
                 172.16.0.0/12, 192.168.0.0/16 }
  }
  set protected6 {
    type ipv6_addr
    flags interval
    elements = { ::/128, ::1/128, ::ffff:0:0/96, fc00::/7, fe80::/10, 64:ff9b::/96,
                 64:ff9b:1::/48, 2002::/16, 2001::/32 }
  }
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "lo" accept
    ct state established,related accept
    ip daddr ${HUB_IP} tcp dport ${HUB_PORT} accept
    ${proxy_rule}
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit } accept
    ip daddr @protected4 counter drop
    ip6 daddr @protected6 counter drop
    ${last_rule:+meta l4proto tcp counter reject with tcp reset}
    ${last_rule:+counter reject}
  }
  chain input {
    type filter hook input priority 0; policy drop;
    iifname "lo" accept
    ct state established,related accept
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert } accept
    counter drop
  }
}
RULES
}

check() {
  output=$(nft list chain inet zeph output) || fail "the rules are not loaded"
  input=$(nft list chain inet zeph input) || fail "the rules are not loaded"
  printf '%s' "$output" | grep -q "ip daddr ${HUB_IP} tcp dport ${HUB_PORT} accept" || fail "no hub rule"
  printf '%s' "$output" | grep -q "ip daddr @protected4 counter" || fail "no IPv4 drop"
  printf '%s' "$output" | grep -q "ip6 daddr @protected6 counter" || fail "no IPv6 drop"
  printf '%s' "$input" | grep -q "policy drop" || fail "the input policy is not drop"
  nft list set inet zeph protected4 | grep -q "192.168.0.0/16" || fail "the IPv4 set is incomplete"
  # The level: a regulated or sealed guard ends its output chain by refusing everything else
  # (a TCP reset, an ICMP error otherwise, so that a client fails at once rather than waiting),
  # an open one does not; only a regulated one lets the egress proxy of the hub through.
  if [ -n "$last_rule" ]; then
    printf '%s' "$output" | tail -n 4 | grep -Eq "^[[:space:]]*meta l4proto tcp counter packets [0-9]+ bytes [0-9]+ reject with tcp reset$" \
      || fail "the output chain of a $EGRESS guard does not end with a refusal"
    printf '%s' "$output" | tail -n 3 | grep -Eq "^[[:space:]]*counter packets [0-9]+ bytes [0-9]+ reject$" \
      || fail "the output chain of a $EGRESS guard does not end with a refusal"
  elif printf '%s' "$output" | grep -q "reject"; then
    fail "an open guard refuses more than the protected networks"
  fi
  if [ -n "$proxy_rule" ]; then
    printf '%s' "$output" | grep -q "$proxy_rule" || fail "no proxy rule"
  elif printf '%s' "$output" | grep -q "ip daddr ${HUB_IP} tcp dport ${PROXY_PORT:-none} accept"; then
    fail "a $EGRESS guard lets the egress proxy through"
  fi
}

case "${1:-run}" in
  run)
    load
    check
    exec sleep infinity
    ;;
  check) check ;;
  load) load && check ;;
  *) fail "usage: guard.sh run|check|load" ;;
esac
