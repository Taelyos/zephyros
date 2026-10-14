# Running Zephyros on one machine

This page describes the deployment on one machine, which is also the one the runtime tests use.

## What runs

| Container | Role |
| --- | --- |
| hub | Holds the contracts, the tokens and the log; serves the operator channel, the main-agent endpoint and, on each instance network, the agent endpoint (MCP tools and the model gateway) |
| filter | The only process that reaches the container runtime. It has no network and serves the hub on a Unix socket, with a closed list of operations ([ADR 0014](decisions/)) |
| guard, one per instance | Loads the network rules of the instance and holds its fixed address. No listener, no secret |
| turn, one per piece of work | The pinned Hermes Agent, as user 10000, in the guard's network namespace, with the instance's two volumes. It ends when its work is done |

An instance is one side (owner or contractor) of one project: a guard, a network and two volumes (the Hermes home and the work directory). The hub starts a turn when it is given work, and a watchdog ends turns that run too long or stop calling the hub. If the hub itself is gone, the filter keeps a reaper: it stops and removes any turn older than `limits.turn_seconds` plus a three-poll grace (so a live hub always decides the fate of its own turns first), one sweep — 30 seconds — behind at the latest (build log, decision 22).

## Start and stop

Needs a Docker-compatible runtime (OrbStack on macOS works) and `docker compose`.

```bash
scripts/deploy.sh up
```

It builds four images (the hub and filter image, the guard, the instance image derived from the pinned Hermes image), writes the configuration to `deploy/state/zephyros.toml` with the images by identifier, and starts the hub and the filter. The operator channel listens on `127.0.0.1:8081`, the main-agent endpoint on `127.0.0.1:8082`. Each guard gives its instance the name resolvers of `network.resolvers` (default `9.9.9.9`, Quad9; override with `ZEPHYROS_RESOLVERS`, a comma-separated list of public addresses), so that instances resolve names without reaching a protected network ([ADR 0018](decisions/0018-instance-name-resolution.md)).

```bash
scripts/deploy.sh call project.create '{"name": "My project"}'
scripts/deploy.sh call instance.start '{"project_id": "p_…", "side": "contractor", "prompt": "…"}'
scripts/deploy.sh call instance.status '{"project_id": "p_…", "side": "contractor"}'
scripts/deploy.sh down
```

`down` stops the hub and the filter, then removes what the filter created outside compose: the guards, turns and sandboxes, and their networks. The instances keep their volumes; the hub creates a guard and a network again when it starts an instance. `down` removes every container and network labelled `org.zephyros.managed` on the host: run one deployment per machine. The model behind the gateway is the obedient stub unless the configuration names a provider (`ZEPHYROS_EXTRA_CONFIG`, see [configuration](configuration.md)).

Both listeners are published on the loopback interface of the machine only, and never listen on an instance network ([ADR 0025](decisions/0025-operator-and-main-agent-off-the-instance-networks.md)). Their tokens are in the hub's data volume (`/var/lib/zephyros/operator.token` and `main-agent.token`, mode 0600) and expire after 90 days by default. To replace one, before or after it expires:

```bash
scripts/deploy.sh rotate-token operator      # `call` reads the new one by itself
scripts/deploy.sh rotate-token main-agent    # then give the main agent the new token
```

The running hub writes the new token to the file and revokes the old one; the turns under way go on ([ADR 0026](decisions/0026-a-lifetime-and-a-rotation-for-the-operator-and-main-agent-tokens.md), [configuration](configuration.md#the-operator-and-main-agent-tokens)).

A deployment made before the core network was named `zephyros-core-<project>` (ADR 0025) keeps its former network after `scripts/deploy.sh up`; remove it with `docker network rm zephyros_core` (`<project>_core` for another `ZEPHYROS_PROJECT`). If the hub then refuses to start with `"core" needs exactly one address`, see [configuration](configuration.md#where-the-operator-channel-and-the-main-agent-endpoint-listen).
