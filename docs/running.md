# Running Zephyros on one machine

This page describes the deployment on one machine, which is also the one the runtime tests use.

## What runs

| Container | Role |
| --- | --- |
| hub | Holds the contracts, the tokens and the log; serves the operator channel, the main-agent endpoint and, on each instance network, the agent endpoint (MCP tools and the model gateway) |
| filter | The only process that reaches the container runtime. It has no network and serves the hub on a Unix socket, with a closed list of operations ([ADR 0014](decisions/0014-container-filter-as-typed-broker.md)) |
| guard, one per instance | Loads the network rules of the instance, at the level of `network.egress`, and holds its fixed address. No listener, no secret |
| sandbox, one per verification | The `tests` kit replaying the acceptance tests on a delivery, with no network |
| turn, one per piece of work | The pinned agent engine (Hermes Agent, or OpenCode for a Contractor configured for it), as user 10000, in the guard's network namespace, with the instance's two volumes. It ends when its work is done |

An instance is one side (owner or contractor) of one project: a guard, a network and two volumes (the Hermes home and the work directory). The hub starts a turn when it is given work, and a watchdog ends turns that run too long or stop calling the hub. If the hub itself is gone, the filter keeps a reaper: it stops and removes any turn older than `limits.turn_seconds` plus a three-poll grace (so a live hub always decides the fate of its own turns first), one sweep — 30 seconds — behind at the latest.

## Start and stop

Needs a Docker-compatible runtime (OrbStack on macOS works) and `docker compose`.

```bash
scripts/deploy.sh up
```

It builds three images (the hub and filter image, the guard, the instance image derived from the pinned Hermes image), a fourth, the image of the `tests` verification kit, when `ZEPHYROS_KIT=tests` is set, and the OpenCode instance image (`images/instance-opencode`) when `ZEPHYROS_CONTRACTOR_ENGINE=opencode` makes the Contractor run OpenCode ([ADR 0029](decisions/0029-the-contractor-can-be-a-third-party-coding-agent.md)). It writes the configuration to `deploy/state/zephyros.toml` with the images by identifier, and starts the hub and the filter. The operator channel listens on `127.0.0.1:8081`, the main-agent endpoint on `127.0.0.1:8082`.

Without `ZEPHYROS_KIT=tests` the hub has no verification runner, and every delivery ends in an error that says so; [a run with real models](real-test.md) gives the whole command.

What an instance reaches is the level of `network.egress` ([ADR 0024](decisions/0024-three-levels-of-network-access.md)); `deploy.sh` reads it from `ZEPHYROS_EGRESS`:

- `regulated`, the default: the hub, and the hosts of `network.allowed_hosts` (`ZEPHYROS_ALLOWED_HOSTS`, comma-separated; by default the package registries) through the egress proxy of the hub, on `hub.egress_port` (8083) of the instance network. The proxy resolves the names and logs every destination as `instance.egress`.
- `sealed`: the hub alone.
- `open`: the hub and every public destination. Each guard then gives its instance the name resolvers of `network.resolvers` (default `9.9.9.9`, Quad9; override with `ZEPHYROS_RESOLVERS`, a comma-separated list of public addresses), so that it resolves names without reaching a protected network ([ADR 0018](decisions/0018-instance-name-resolution.md)).

No level reaches a protected network. A guard whose level differs from the configuration (one made before version 0.2.0, or a level changed since) is stopped when the hub starts or finds it, so that the next turn starts at the configured level.

A project that needs heavy, fixed tooling (a simulator, a test bench and its scripts) gets it from a directory of this machine: `ZEPHYROS_TOOLS=/srv/zephyros-tools` mounts it read-only at `/opt/project-tools` in every turn and every sandbox, with its `bin` on the `PATH`, after the same check the hub makes when it starts ([the tools of a project](configuration.md#the-tools-of-a-project), [ADR 0030](decisions/0030-the-tools-of-a-project-are-mounted-read-only.md)). A verification may then need more than its default 300 s: `verification.timeout_seconds` in `ZEPHYROS_EXTRA_CONFIG` goes up to 86 400.

```bash
scripts/deploy.sh call project.create '{"name": "My project"}'
scripts/deploy.sh call instance.start '{"project_id": "p_…", "side": "contractor", "prompt": "…"}'
scripts/deploy.sh call instance.status '{"project_id": "p_…", "side": "contractor"}'
scripts/deploy.sh down
```

Each turn of an instance continues its conversation ([ADR 0016](decisions/0016-one-agent-container-per-turn.md)). When a conversation has gone wrong (an agent lost in summaries of summaries, a session that keeps failing), the operator starts the next turn in a new one: `"session": "new"` in `instance.start`. That turn and the following ones work in the new session. The earlier ones stay on the home volume; no turn continues them, but Hermes can still search them (`session_search`). The work volume keeps the work, and what the engine keeps beside the conversation stays and is read again: the memory of Hermes (`MEMORY.md`, `SOUL.md`), its skills, and the `AGENTS.md` of the work directory. A new session is a fresh conversation, not a fresh agent. Only the operator may ask for it (`forbidden` for the main agent), and each `turn.start` in the log records `session`, `continue` or `new`.

```bash
scripts/deploy.sh call instance.start '{"project_id": "p_…", "side": "owner", "prompt": "…", "session": "new"}'
```

An agent that is blocked by what it cannot change reports an obstacle; the Owner side answers those of the Contractor, and escalates to the operator what only the operator can act on ([ADR 0034](decisions/0034-a-blocked-agent-reports-an-obstacle-up-a-chain.md)). The main agent hears of each obstacle that reaches the operator or the human (`obstacle.raised`, [notifications](notifications.md)). The operator follows and answers them on its channel:

```bash
scripts/deploy.sh call obstacle.list '{"status": "awaiting", "level": "human"}'   # every project
scripts/deploy.sh call obstacle.read '{"project_id": "p_…", "obstacle_id": "OB-001"}'
scripts/deploy.sh call obstacle.answer '{"project_id": "p_…", "obstacle_id": "OB-001", "decision": "handle", "action": "new_session", "text": "…"}'
```

`handle` records what the operator did: a new turn (`relaunched`), a new session (`new_session`, with `instance.start` and `"session": "new"`), a stop (`stopped`), a fixed environment (`environment_fixed`). The answer does not act by itself. The Contractor reads every answer to its obstacles: say what an acceptance test expects in your own words, never with its code.

`down` stops the hub and the filter, then removes what the filter created outside compose: the guards, turns and sandboxes, and their networks. The instances keep their volumes; the hub creates a guard and a network again when it starts an instance. `down` removes every container and network labelled `org.zephyros.managed` on the host: run one deployment per machine. The model behind the gateway is the obedient stub unless the configuration names a provider (`ZEPHYROS_EXTRA_CONFIG`, see [configuration](configuration.md)).

Both listeners are published on the loopback interface of the machine only, and never listen on an instance network ([ADR 0025](decisions/0025-operator-and-main-agent-off-the-instance-networks.md)). Their tokens are in the hub's data volume (`/var/lib/zephyros/operator.token` and `main-agent.token`, mode 0600) and expire after 90 days by default. To replace one, before or after it expires:

```bash
scripts/deploy.sh rotate-token operator      # `call` reads the new one by itself
scripts/deploy.sh rotate-token main-agent    # then give the main agent the new token
```

The running hub writes the new token to the file and revokes the old one; the turns under way go on ([ADR 0026](decisions/0026-a-lifetime-and-a-rotation-for-the-operator-and-main-agent-tokens.md), [configuration](configuration.md#the-operator-and-main-agent-tokens)).

A deployment made before the core network was named `zephyros-core-<project>` (ADR 0025) keeps its former network after `scripts/deploy.sh up`; remove it with `docker network rm zephyros_core` (`<project>_core` for another `ZEPHYROS_PROJECT`). If the hub then refuses to start with `"core" needs exactly one address`, see [configuration](configuration.md#where-the-operator-channel-and-the-main-agent-endpoint-listen).
