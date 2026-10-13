#!/bin/sh
# Spike Z2-02: the network guard. It owns the network namespace that an instance joins, and
# loads the egress and ingress rules into it. Usage: guard.sh run | load | flush
#   env HUB_IP, HUB_PORT   the one permitted destination (address, port, TCP)
#   env HUB_IN_PORT        a port on which the hub may open connections toward the instance
#   env DELAY              seconds to wait before loading the rules (readiness-race experiment)
set -eu

load() {
  # Only our own table is touched: Docker keeps the redirect of its embedded DNS resolver in an
  # "ip nat" table of this namespace, and a "flush ruleset" would silently break name resolution.
  nft -f - <<RULES
table inet zeph
delete table inet zeph
table inet zeph {
  set protected4 {
    type ipv4_addr
    flags interval
    elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16 }
  }
  set protected6 {
    type ipv6_addr
    flags interval
    elements = { ::1/128, fc00::/7, fe80::/10, 64:ff9b::/96, 64:ff9b:1::/48, 2002::/16, 2001::/32 }
  }
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "lo" accept
    ct state established,related accept
    ip daddr ${HUB_IP} tcp dport ${HUB_PORT} counter accept
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit } accept
    ip daddr @protected4 counter drop
    ip6 daddr @protected6 counter drop
  }
  chain input {
    type filter hook input priority 0; policy drop;
    iifname "lo" accept
    ct state established,related accept
    ip saddr ${HUB_IP} tcp dport ${HUB_IN_PORT} counter accept
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert } accept
    counter drop
  }
}
RULES
}

case "${1:-run}" in
  load) load ;;
  flush) nft delete table inet zeph ;;
  run)
    sleep "${DELAY:-0}"
    load
    : > /tmp/ready
    exec sleep infinity
    ;;
  *) echo "usage: guard.sh run|load|flush" >&2; exit 2 ;;
esac
