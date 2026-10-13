# 0018. An instance resolves names through configured resolvers, never through the host's resolver chain

- Status: Proposed
- Date: 2026-10-12

## Context

The network guard of an instance blocks every connection to a protected network (annex "Protected networks" of the statement of work), except the agent endpoint of the hub. The instance still needs to resolve public names: it works on code, installs packages and reads documentation.

On a host whose own name resolvers are inside a protected network — a homelab with local DNS (CoreDNS, Pi-hole, AdGuard, a router at 192.168.x.x), which is the target machine class of EXG-31 — the resolution chain of the container runtime forwards the instance's queries from the instance's network namespace toward those resolvers. The guard drops those packets, and no public name resolves from the instance.

Measured on the Linux development host (Debian 13, Docker 26.1.5, resolvers on the LAN):

- one cold lookup emits 18 packets toward the LAN resolvers, all dropped by the guard (the `nft` counter of the guard's rules);
- a name-based fetch fails with `EAI_AGAIN` after about 5 s;
- permitting `dport 53` toward the resolvers makes the same fetch succeed, which isolates the guard as the cause;
- with the rules removed entirely, resolution works, but a cold name costs about 4 s through the host's resolver chain.

Consequences for the suites on such a host: the ATK-44 positive control ("a public destination stays reachable") fails, and ATK-41 is skipped because the public wildcard name cannot resolve.

## Decision

- The configuration gains `network.resolvers`: the name resolvers the instances use, one to four IPv4 addresses. The guard of an instance carries them as its DNS, and the turn, which joins the guard's network namespace, uses them.
- The default is `9.9.9.9` (Quad9), chosen by the Owner on 2026-10-12: a public resolver that filters malicious domains and does not log the client address. A deployment overrides it (`ZEPHYROS_RESOLVERS` for `scripts/deploy.sh`, or the key in the configuration file).
- A resolver address inside a protected network is refused at startup, naming the key: the guard would block it, so it can never work (EXG-50 says the refusal names the faulty key). This is what keeps the default honest on the homelab.
- No guard rule and no contract text change: a public resolver is a destination the annex already permits ("the public Internet stays open to instances").

## Consequences

- Positive: instances resolve names on every supported platform, homelab resolvers included; the behavior no longer depends on where the host's resolver sits; the misconfiguration that cannot work (a protected resolver) stops the start instead of failing at the first model call.
- Negative: by default, the lookups of an instance go to a third party (Quad9) rather than to the user's own resolver. A user who wants no third party must either deploy a resolver reachable without a protected address, or wait for the amendment sketched in the alternatives.
- Neutral: the failure that motivated this ADR was found by the runtime suite on Linux; the contract and the threat model are untouched.

## Alternatives considered (all measured on the same host)

- **Suppress or short-circuit AAAA queries.** No effect: the block hits IPv4 lookups too. The failure is not in the address family.
- **Raise the timeout of the probe.** No effect: name resolution is not bounded by the socket timeout, and the lookup fails whether the fetch waits 2 s or 30 s.
- **A guard exception for the host's resolvers.** Works (the fetch passes; the wildcard name resolves; a cold name costs about 4 s), and it keeps the user's own DNS. But annex "Protected networks" states today that a resolver reached at a protected address is not a permitted destination, so this needs an amendment and a guard rule change. Recorded as a candidate for the Owner.
- **A recursive resolver inside the instance, on its own loopback.** The annex already permits "a name resolver that is served on the loopback interface of the execution context itself", and no third party is involved. Kept for a later milestone: every turn would carry another process, and it depends on outbound port 53, which some networks filter.
