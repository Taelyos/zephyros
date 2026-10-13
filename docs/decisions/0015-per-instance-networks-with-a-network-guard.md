# 0015. Each instance has its own network and a network guard that enforces egress
- Status: Accepted
- Date: 2026-10-09

## Context
EXG-03 and EXG-41 need egress control that an instance cannot change, on Linux and on macOS. Spike Z2-02 showed that a small container per instance, holding the rules in a namespace that the instance shares but cannot modify, enforces them on both platforms.

## Decision
Each instance has its own network, with the hub attached to it. A network guard container per instance loads the rules. It keeps only the NET_ADMIN capability, with no-new-privileges, a read-only root and limits, and it is not an execution context. The hub starts an instance once its guard is healthy and stops it when the guard dies (fail-closed). The hub and the container filter run in containers. The filter limits every operation, reads included, to Zephyros-labelled objects, and only the hub reaches it, through a Unix socket.

## Consequences
- Positive: the rules sit below the instance, so a hijacked agent cannot lift them.
- Positive: the names of other instances do not resolve.
- Negative: on macOS (OrbStack) Docker networks are not isolated from each other, so the guard rules are the only barrier between projects and between sides. Linux adds the runtime's isolation.
- Negative: the default address pool runs out after about 23 networks, so the system allocates a small explicit subnet to each.

## Alternatives considered
- A shared network with guard rules only: instances could resolve the names of other guards.
- Host firewall rules: not enforceable on macOS, where the host sees all container traffic as one process.
