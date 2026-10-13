# 0014. The container filter is a typed broker, not a pass-through proxy
- Status: Accepted
- Date: 2026-10-09

## Context
EXG-34 exists because a hijacked hub could create a privileged container or mount the host. A proxy that forwards container-runtime API calls after checking them leaves the hub free to build any specification, so the filter has to understand every field of every request, and an unknown field or a parsing difference between the filter and the runtime becomes a bypass. The catalog attacks exactly that: alternate and unversioned paths, URL-encoded segments, chunked bodies, method override (ATK-73), and injected options through project parameters (ATK-74).

## Decision
The filter runs as a separate process in its own container and is the only holder of the runtime socket. It has **no network listener**: its container has the network `none`, and the hub reaches it through a Unix socket on a volume shared between the two containers. The filter is hardened as the equivalent of host root, because anything that can speak to it can start containers.

The hub sends it small typed requests (for example "create the instance container of this project and side, with this address") and the filter builds the full container specification from templates it owns. In Z2 images come only from digests configured locally. Every Zephyros object carries labels set by the filter. Requests shaped like raw runtime API calls, requests with unknown or duplicate fields, and every operation outside annex "Permitted container operations" are refused and written to the filter's own journal. In Z2 that journal is a simple append-only file, not a hash chain.

The filter has a template for the network guard container (amendment A5, see ADR 0015).

## Consequences
- Positive: the hub never produces a runtime specification, so a hijacked hub can ask only for the kinds the templates allow.
- Positive: with no network listener, nothing on any network can reach the filter; the shared socket is the only door.
- Positive: the surface to review is a short list of templates and a small strict parser, not the runtime API.
- Negative: a new component to build and keep in step with the runtime.
- Negative: any new kind of container (a sandbox, in Z3) needs a new template reviewed by the Owner.
- Negative: a hijacked hub can still start the allowed kinds in the allowed numbers; the limits come from the configuration the filter reads, not from the hub.

## Alternatives considered
- A pass-through proxy with an allow-list on method, path and body (the common socket-proxy pattern).
- Calling the runtime from the hub with a code-level template and no separate process: a hijacked hub could bypass it.
- A TCP listener for the filter on a private network: reachable by anything on that network.
