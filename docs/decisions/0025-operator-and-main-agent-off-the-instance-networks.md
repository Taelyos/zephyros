# 0025. The operator channel and the main-agent endpoint are off the instance networks by construction

- Status: Proposed
- Date: 2026-10-14
- Refines: [0015](0015-per-instance-networks-with-a-network-guard.md)

## Context

The operator channel (port 8081) and the main-agent endpoint (port 8082) carry every right of the hub. No execution context may reach them (ATK-13). The hub container is attached to its core network, which carries the publication of these two ports on the loopback interface of the machine, and, by the filter, to the network of every instance, where its agent endpoint (8080) must stay reachable.

Until now `scripts/deploy.sh` made both listen on `0.0.0.0`. They therefore answered on the hub's address of every instance network, and only the guard of each instance kept the instance away from them. The check of the `Host` header is no barrier: an instance writes the header it likes. A guard that is absent, stopped or wrong would have opened the two most powerful listeners of the hub to an agent.

A measure on Docker 26 also showed why `0.0.0.0` had been needed: the runtime publishes the ports of a container on the network that carries its default route, and picks that network by the alphabetical order of the network names. A deployment whose compose project sorts after `zs-net-` (the test deployments, `zsrt…`) saw its publication move to the hub's address on the first instance network attached.

The project owner decided on 2026-10-14 that these two listeners need a second barrier that holds without any guard.

## Decision

1. **Listen where the publication arrives, and nowhere else.** `hub.operator_bind` and `hub.main_agent_bind` accept a new value, `"core"`, which `scripts/deploy.sh` writes. When the hub starts, it reads the IPv4 addresses of its network namespace and keeps the one that is neither a loopback nor inside `network.instance_pool`: its address on the core network. Every instance network is taken from the pool, so the listener cannot be on one. The hub also listens on the loopback of its own container (its health check and `scripts/deploy.sh call` use it; nothing outside the container reaches that loopback). If there is not exactly one such address, the hub refuses to start, before anything opens.
2. **The schema refuses the dangerous values.** A wildcard address (`0.0.0.0`, `::`) is refused for both keys, and so is an address inside the instance pool.
3. **The core network keeps the publication.** `deploy/compose.yaml` names the core network `zephyros-core-<project>`, which sorts before every instance network (`zs-net-…`) whatever the project is called, so the runtime keeps the default route, and the publication, on the core network once the hub joins the instance networks.
4. **A peer of the instance pool is refused.** Both listeners refuse, before reading any token, a connection whose TCP peer address is inside the instance pool, and log it (`wrong_source`). This is meant for a runtime that routes between its networks (ADR 0015 notes that OrbStack does not isolate them). It is not measured on macOS; if the runtime hides the source address between networks, the guard is the only barrier.

The proof runs on the real runtime (`tests/runtime/test_atk_13_listeners.py`): a whole Zephyros, an instance network made the way the filter makes it, the hub attached to it at its fixed address, and **no guard**. A probe without any capability, on that network, finds 8081 and 8082 refused on the hub's address there, the hub's core address and the published ports out of reach, and 8080 open; from the machine, the two published ports still answer. The same holds after a restart of the hub, which then starts with the instance network already attached. With the former wildcard, the same test failed: both ports were open from the instance network. A control in the suite keeps showing that the probe sees that defect: a listener on the wildcard address in the hub's network namespace is found open from the instance network, and one on the core address is not.

## Consequences

- Positive: the barrier is the kernel's: nothing listens on 8081 or 8082 at an address of an instance network. A missing or broken guard no longer exposes them.
- Positive: a configuration that would listen on an instance network is refused when the hub starts, with the key named.
- Positive: the publication no longer depends on the name of the compose project.
- Negative: a configuration written by hand with `0.0.0.0` for either key no longer loads (schema 1.15.0); `"core"` replaces it. `scripts/deploy.sh` writes `"core"`.
- Negative: an existing deployment gets a new core network name: the next `scripts/deploy.sh up` recreates the hub on `zephyros-core-<project>`, and the former network, `<project>_core`, is left behind for `docker network rm`.
- Negative: the choice of the network that keeps the publication rests on the runtime's ordering of names, as measured on Docker 26. Docker 28 adds a gateway priority that could state it explicitly; until the supported runtimes all have it, the name does the work, and the runtime test checks the result on the runtime the CI uses. Were the ordering to change, the two listeners would become unreachable from the machine, never reachable from an instance.
- Neutral: on Linux, the runtime's isolation between bridge networks already drops what an instance sends to the hub's core address; the refusal of item 4 is what holds where the runtime does not isolate. An instance that could route through the hub's address on its own network would need NET_ADMIN, which no turn has.

## Alternatives considered

- **A fixed address for the hub on the core network** (a fixed subnet in compose): explicit, but the subnet can collide with a network of the machine, and the configuration must repeat it.
- **A relay container on the core network alone** that forwards the published ports to the hub: one more container, and the hub would see every call come from the relay.
- **A separate network for the publication**: the same choice of default route as the core network, with nothing gained.
- **Rely on the guards alone**: the situation this decision ends.
