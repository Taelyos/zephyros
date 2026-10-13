# Configuration

The hub reads one TOML file. It validates the file against a versioned schema and **refuses to start** when the file violates it, naming each faulty key.

```bash
zephyros config check zephyros.toml     # validate a file
zephyros config schema                  # print the JSON Schema
```

The schema is published as [`schemas/configuration.schema.json`](../schemas/configuration.schema.json), version 1.7.0. A file declares the version it was written for in `schema_version`; the hub accepts any 1.x file and refuses another major version. Unknown keys are refused.

A minimal file:

```toml
schema_version = "1.7.0"

[images]
instance = "registry.example/zephyros-instance@sha256:…"   # by digest, always
guard = "sha256:…"   # or a local image by its identifier
```

| Section | What it holds |
| --- | --- |
| `[hub]` | The three listeners: the agent endpoint (the one hub destination open to instances), the operator channel and the main-agent endpoint. The last two bind to a host address and are never reachable from an instance |
| `[paths]` | The data directory, the Unix socket of the container filter, and the files that hold the operator and main-agent tokens |
| `[runtime]` | The name of the core network and of the hub container; how long the hub waits for a network guard to report healthy, and how often it looks at its instances |
| `[images]` | The instance image and the network guard image, by registry digest (`name@sha256:…`) or by local identifier (`sha256:…`) (required) |
| `[network]` | The range the subnets of the instance networks are taken from, their prefix length, and the name resolvers the instances use (one to four public addresses; the default is `9.9.9.9`, Quad9). A resolver inside a protected network is refused at startup, because the network guard blocks it ([ADR 0018](decisions/0018-instance-name-resolution.md)) |
| `[limits]` | CPUs, memory and process count of an agent container, memory of a guard, the longest duration of a turn, and how long a turn may go without calling the hub before it is stopped |
| `[tokens]` | The lifetime of a project token and how long before its expiry a new turn gets a new one |
| `[forge]` | Where pull requests are opened: `none`, `directory` (bare git repositories in a directory, for the development deployment and the tests), or `github` (the repositories of the person, through the API and git over https, with the file of its token in `token_file`; read at each call) |
| `[verification]` | What runs the acceptance tests of a delivery (`none`, or `sandbox`), the verification kit, and the longest run |
| `[gateway]` | The model provider behind the gateway (`stub`, or `openai-compatible` with a URL and a credential file), the model name instances ask for, optionally the name of the model at the provider, and `simulation` to mark the report, the pull request and the events of a run with the stub (EXG-46), and the name of the session header the gateway sends to the provider (`session_header`, for OpenCode Go's `x-opencode-session`; one stable session name per hub process), and how long the gateway waits for one answer of the provider (`timeout_seconds`, 600 by default: a long answer of a real model takes minutes). The credential file is read at each call, so changing it takes effect on the next call |
| `[notifications]` | Where the hub posts its signed notifications to the main agent, and the shared secret that signs them ([docs/webhooks.md](webhooks.md), [docs/notifications.md](notifications.md)); both keys are set together, empty turns them off |
| `[continuity]` | The escalation ladder of a task without activity (US-E7-01): how long before the instance is woken and restarted, how many restarts before the main agent is alerted, the delay the main agent has before the human is alerted, and how many rejected deliveries on one branch alert the main agent (EXG-23) |

Some rules cross keys, and the schema document lists them: the three ports differ, a token outlives the longest turn, the subnet prefix is longer than the pool's, an OpenAI-compatible provider needs its URL and its credential file, and the notifications are set together with an http(s) url and a secret of at least 16 characters.

Versions: 1.1.0 added `runtime.guard_health_seconds`, `runtime.poll_seconds` and `limits.silence_seconds`; 1.2.0 added `[forge]` and `[verification]`, and local image identifiers; 1.3.0 added `network.resolvers` (default `9.9.9.9`, Quad9); 1.4.0 added `[notifications]` and the signed webhooks (EXG-43); 1.5.0 added `[continuity]` (US-E7-01, EXG-23); 1.6.0 added `limits.refusals_per_minute` (ATK-71); 1.7.0 added `gateway.simulation` (EXG-46); 1.8.0 added `gateway.models` and the authorised-models policy; 1.9.0 added the GitHub forge (`forge.provider` `github` and `forge.token_file`); 1.10.0 added `gateway.session_header`; 1.11.0 added `gateway.timeout_seconds`; 1.12.0 added `verification.protected_paths` and raised the defaults of `limits.memory_mib` (1536) and `limits.turn_seconds` (7200) to the measures of the second real run (ADR 0020). Each addition has a default, so a 1.0.0 file still reads.
