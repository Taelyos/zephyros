# 0024. Three levels of network access for instances; regulated by default

- Status: Proposed
- Date: 2026-10-14
- Supersedes: [0009](0009-network-access-policy.md)

## Context

ADR 0009 gave every instance the whole public Internet, with no route into private networks, and no mandatory egress proxy. Three things make that the wrong default:

- **Leaks.** An instance holds the client's code and a project token. With the whole Internet open, a manipulated agent can send either to any host, and nothing records where it went.
- **Hostile content.** A page the agent reads can carry instructions. The more of the web an instance reaches, the more of that content it reads, and the more places it can be told to send things.
- **Integrity of the verification.** The gate judges what the Contractor built. An instance that can fetch the answer (an upstream fix, a published test suite) defeats it: on 2026-10-13, two Contractor instances of a comparison run fetched the upstream fix or the hidden tests from GitHub with their web tool.

The program lead decided on 2026-10-14 that the operator chooses one of three levels, and that the default is the restrictive one that still lets ordinary work install packages. The product contract said "the public Internet stays open to instances"; this decision replaces that sentence.

## Decision

`network.egress` in the configuration sets the level of every instance, Owner and Contractor alike.

| Level | What an instance reaches | Use |
| --- | --- | --- |
| `regulated` (default) | The hub, so the model through its gateway, and the hosts of `network.allowed_hosts` on port 443 through the egress proxy of the hub; by default the package registries (`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`). Everything else is refused. Every destination asked for, allowed or refused, is logged by the hub | Normal use |
| `sealed` | The hub alone. The instance image carries what ordinary work needs: Python with pytest and its wheels, at the versions and hashes of the `tests` kit | A benchmark, a sensitive project |
| `open` | Every public destination, as ADR 0009 had it | An explicit exception; the hub logs a warning each time a turn of such an instance starts |

At every level the protected networks of ADR 0009 stay unreachable (private ranges, loopback, link-local, IPv6 unique local and link-local, the shared address space, cloud metadata). GitHub is not in the default list; an operator who wants it adds it. A sandbox of verification has no network, whatever the level.

### The mechanism

- **The guard holds the level** (ADR 0015). It loads the rules of the configured level into the network namespace the turn joins. At `regulated` and `sealed` its output chain ends by refusing everything else (a TCP reset, an ICMP error for other protocols, so that a client fails at once and says why rather than waiting for its timeout): a turn reaches the hub's agent endpoint, and at `regulated` also the hub's egress proxy (`hub.egress_port`, 8083 by default), and nothing else, name resolvers included. At `open` the chain is ADR 0009's. The health check of the guard states the level (a regulated or sealed guard without its final refusals is unhealthy, and the hub stops an instance whose guard is unhealthy), and the filter refuses a guard specification whose level is not the configured one. The turn has no capability, so it cannot change any of this.
- **A change of level reaches the existing instances.** A guard loads its rules once, when it starts, and carries its level as a label (`org.zephyros.egress`). When the hub starts, and each time it looks at an instance, it takes down any guard whose level is not the configured one, a guard made before the levels existed included (it has no label), and logs it (`instance.egress`, `why: level_changed`). The volumes stay; the next start of that instance brings it up at the configured level. So changing `network.egress` and restarting the hub never leaves an instance on the old rules; it stops the turns under way.
- **The egress proxy is in the hub.** A regulated turn gets `HTTPS_PROXY` (and `HTTP_PROXY`, both spellings) pointing at it, with its project token as the proxy password, and `NO_PROXY` for the hub itself. `pip`, `npm`, `uv`, `curl` and most HTTP clients follow it. For each connection the proxy reads one `CONNECT host:443`, authenticates the token exactly as the agent endpoint does (bound to the address of the guard), compares the host with the allow-list by name, resolves the name itself, refuses it when any of its addresses is in a protected network, then connects to the first of the checked addresses that answers (it never resolves again) and relays bytes it never reads, for as long as bytes flow in either direction (one idle clock for both, five minutes), passing a half-close on. A plain proxied request (`GET http://…`) is refused.
- **The log.** Each destination asked for through the proxy is one record `instance.egress` in the stream of the project, under the instance: `ok` (relayed), `refused` (with `why`: `not_allowed`, `port`, `not_connect`, `protected`, `no_address`), or `error` (allowed, but the host could not be reached). The record holds the host, the port, the side, the source address and the level, never anything that travelled in the tunnel. At `open`, each turn start writes an `instance.egress` record with `level: open` and a warning.

Why this mechanism:

- **By name, not by address.** The registries sit behind content delivery networks whose addresses change and are shared with other sites. A rule on resolved addresses would either break when they move or, to stay working, open whole ranges of the CDN to everything else on it. A proxy that is asked a name decides on the name.
- **The proxy in the hub, not in the guard.** The hub already authenticates every call of an instance, by a token bound to the guard's address, and already holds the log. A proxy in the guard would have no secret to authenticate with and no way to write to the hub's log except a new channel from every guard to the hub. In the hub, one listener serves every instance network, as the agent endpoint does, and the guard needs one more accept rule.
- **CONNECT only.** The registries are https. A tunnel keeps the TLS of the client end to end: the hub never holds a certificate of its own, never sees what is fetched, and the client still checks the registry's certificate. It works the same on Linux and on macOS (OrbStack): the guard rules are the ones ADR 0015 already relies on, and the proxy is ordinary TCP from the hub's container.

## Consequences

- Positive: by default an instance can send nothing to a host of its choosing, and every attempt through the proxy is in the hub's log, attributed to the instance.
- Positive: an instance cannot look the answer up: GitHub and the rest of the web are out of reach unless the operator lists them.
- Positive: `sealed` gives a benchmark or a sensitive project an instance whose only peer is the hub, and the image still runs the project's tests.
- Positive: name resolution no longer leaves a regulated or sealed instance at all; the proxy resolves through the hub. The name resolvers of ADR 0018 (`network.resolvers`) now serve the `open` level only; the key stays required, so that a change to `open` needs nothing more.
- Negative: **an allowed registry is still a channel.** A manipulated agent can encode data in the names or paths it asks a registry for, and the registry's operator (or anyone who can publish there) sees them. The log shows the host, not the path. Package installation also runs code from the registry inside the instance. The list is kept short for that reason.
- Negative: **the proxy does not make a registry read-only.** It opens a tunnel to an allowed host whatever the request inside: an upload (`PUT`, `npm publish`) with a credential the agent was given or found would pass. The instances hold no registry credential of Zephyros'.
- Negative: **the proxy checks the CONNECT name, not what goes inside the tunnel.** A client could open a tunnel to `pypi.org` and send a TLS `Host` for another site served by the same CDN (domain fronting). The large CDNs refuse a mismatched `Host`, but this rests on them, not on Zephyros.
- Negative: a client that ignores `HTTPS_PROXY` reaches nothing, and its attempts are dropped by the guard without a record in the hub's log (the guard's counters see them). The web tools of Hermes reach no arbitrary page either way: through the proxy they are refused, around it they are dropped.
- Negative: the token of the turn is in the proxy variables of its environment. It already was in the environment (`ZEPHYROS_TOKEN`); a token is bound to the address of the guard, so it is no use elsewhere.
- Negative: the instance image now carries a Python with pytest first on the `PATH` (3.14, the image's own, while the `tests` kit runs 3.12): a project whose tests depend on the minor version of Python may behave differently in the instance and in the sandbox.
- Neutral: the hub's own egress (EXG-48, `egress.refused`) is unchanged; the hosts of `allowed_hosts` are hosts the configuration names, and the hub reaches them only on behalf of a regulated instance.

## Alternatives considered

- **Keep ADR 0009 (everything open).** Rejected for the three reasons of the context.
- **A guard rule per resolved address of each allowed host.** Fragile with CDNs, and it opens the CDN's other tenants; it also needs the guard to resolve names and refresh its rules.
- **A proxy inside each guard.** Rules out of reach of the instance as well, but the proxy would have no way to authenticate the instance or to write the hub's log without a new channel; the hub would have to collect logs from every guard.
- **A TLS-intercepting proxy** (to see paths and refuse uploads). It would need a certificate authority trusted by every instance and would put the content of every request in the hub. Out of proportion for read-only registries.
- **A mirror of the registries in the hub.** The strongest answer to the channel above, and a large amount of work and storage; recorded as a later option.
