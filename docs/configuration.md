# Configuration

The hub reads one TOML file. It validates the file against a versioned schema and **refuses to start** when the file violates it, naming each faulty key and saying why.

```bash
zephyros config check zephyros.toml     # validate a file
zephyros config schema                  # print the JSON Schema
```

The schema is published as [`schemas/configuration.schema.json`](../schemas/configuration.schema.json), version 1.13.0. A file declares the version it was written for in `schema_version`; the hub accepts any 1.x file and refuses another major version. Unknown keys are refused.

A minimal file:

```toml
schema_version = "1.13.0"

[images]
instance = "registry.example/zephyros-instance@sha256:…"   # by digest, always
guard = "sha256:…"   # or a local image by its identifier
```

On one machine, `scripts/deploy.sh up` writes the file (`deploy/state/zephyros.toml`) with the version the code speaks and the images it built, then appends `ZEPHYROS_EXTRA_CONFIG` ([running Zephyros](running.md), [a run with real models](real-test.md)).

## Sections

| Section | What it holds |
| --- | --- |
| `[hub]` | The three listeners: the agent endpoint (the one hub destination open to instances), the operator channel and the main-agent endpoint. The last two bind to a host address and are never reachable from an instance |
| `[paths]` | The data directory, the Unix socket of the container filter, and the files that hold the operator and main-agent tokens |
| `[runtime]` | The name of the core network and of the hub container; how long the hub waits for a network guard to report healthy, and how often it looks at its instances |
| `[images]` | The instance image and the network guard image, by registry digest (`name@sha256:…`) or by local identifier (`sha256:…`) (required) |
| `[network]` | The range the subnets of the instance networks are taken from, their prefix length, and the name resolvers the instances use (one to four public addresses; the default is `9.9.9.9`, Quad9). A resolver inside a protected network is refused at startup, because the network guard blocks it ([ADR 0018](decisions/0018-instance-name-resolution.md)) |
| `[limits]` | CPUs, memory and process count of an agent container, memory of a guard, the longest duration of a turn, how long a turn may go without calling the hub before it is stopped, and how many refused actions of one instance in a minute make the hub answer without doing the work (`refusals_per_minute`, ATK-71) |
| `[tokens]` | The lifetime of a project token and how long before its expiry a new turn gets a new one |
| `[forge]` | Where pull requests are opened: `none`, `directory` (bare git repositories in a directory, for the development deployment and the tests), or `github` (the repositories of the person, through the API and git over https, with the file of its token in `token_file`; read at each call) |
| `[verification]` | What runs the acceptance tests of a delivery (`none`, which ends every verification in an error, or `sandbox`), the verification kit and its image, the longest run, and the client's own files (`protected_paths`, `CONTRACT.md` by default): a delivery that changes one is flagged in its report and its pull request, never refused ([ADR 0020](decisions/0020-the-owner-sees-and-records.md)) |
| `[gateway]` | The model provider behind the gateway (`stub`, or `openai-compatible` with a URL and a credential file), the model name instances ask for, the name of the model at the provider when it differs, the models the gateway may serve (`models`, see below), `simulation` to mark the report, the pull request and the events of a run with the stub (EXG-46), the name of the session header the gateway sends to the provider (`session_header`, for OpenCode Go's `x-opencode-session`; one stable session name per hub process), and how long the gateway waits for one answer of the provider (`timeout_seconds`: a long answer of a real model takes minutes). The credential file is read at each call, so changing it takes effect on the next call |
| `[notifications]` | Where the hub posts its signed notifications to the main agent, and the shared secret that signs them ([docs/webhooks.md](webhooks.md), [docs/notifications.md](notifications.md)); set both keys, or neither: both empty turn the notifications off |
| `[continuity]` | The escalation ladder of a task without activity (US-E7-01): how long before the instance is woken and restarted, how many restarts before the main agent is alerted, the delay the main agent has before the human is alerted, and how many FAIL decisions in a row on one project alert the main agent (`rejections`, EXG-23) |

## The models the gateway serves

The instances ask the gateway for `gateway.model`; the gateway sends the provider `gateway.upstream_model`, or `gateway.model` itself when `upstream_model` is unset. With an `openai-compatible` provider, `gateway.models` lists the models the gateway may send, and the hub refuses to start when the one it would send is not in the list.

That list is the model policy of the deployment, and it lives in the configuration alone: the code authorises no model of its own. Naming one model authorises that one alone — the three real runs named DeepSeek V4.1 Flash alone:

```toml
[gateway]
provider = "openai-compatible"
upstream_url = "https://provider.example/v1"
credential_file = "/run/zephyros-credentials/gateway.key"
model = "served-model"
upstream_model = "deepseek-v4.1-flash"
models = ["deepseek-v4.1-flash"]
```

With this table, a file that sets `upstream_model = "claude-opus-5-5"` refuses to start:

```text
gateway.upstream_model: 'claude-opus-5-5' is not one of gateway.models (deepseek-v4.1-flash): the gateway serves only the models its configuration names
```

The policy holds only as long as nobody else edits the table: keep it in a file that only you can change, outside the repository ([a run with real models](real-test.md)).

## Rules across keys

The schema document lists them (`x-zephyros-rules`):

- the three ports of `[hub]` differ;
- a project token outlives the longest turn, and its rotation starts before its expiry;
- the subnet prefix of an instance network is longer than the prefix of the pool;
- no name resolver is in a protected network;
- a `github` forge has its `token_file`;
- an `openai-compatible` provider has its `upstream_url` and its `credential_file`, and the model it sends is one of `gateway.models`;
- a `sandbox` runner has its `kit_image`;
- `gateway.simulation` runs only with the `stub` provider;
- the notifications have both their URL (http or https) and their secret (16 characters at least), or neither.

## Every key and its default

A key that a file leaves out takes its default. "Unset" means that the key has no default and that nothing uses it until the file sets it.

| Key | Default | Values |
| --- | --- | --- |
| `schema_version` | required | a version, `1.x.y` |
| `hub.agent_port` | `8080` | 1024 to 65535 |
| `hub.operator_port` | `8081` | 1024 to 65535 |
| `hub.main_agent_port` | `8082` | 1024 to 65535 |
| `hub.operator_bind` | `"127.0.0.1"` | an IP address |
| `hub.main_agent_bind` | `"127.0.0.1"` | an IP address |
| `paths.data_dir` | `"/var/lib/zephyros"` | an absolute path |
| `paths.filter_socket` | `"/run/zephyros/filter.sock"` | an absolute path |
| `paths.operator_token_file` | `"/var/lib/zephyros/operator.token"` | an absolute path |
| `paths.main_agent_token_file` | `"/var/lib/zephyros/main-agent.token"` | an absolute path |
| `runtime.core_network` | `"zephyros-core"` | a name |
| `runtime.hub_container` | `"zephyros-hub"` | a name |
| `runtime.guard_health_seconds` | `60` | 5 to 600 |
| `runtime.poll_seconds` | `5` | 1 to 60 |
| `images.instance` | required | an image, by digest or local identifier |
| `images.guard` | required | an image, by digest or local identifier |
| `network.instance_pool` | `"10.200.0.0/16"` | an IPv4 network |
| `network.instance_prefix` | `28` | 24 to 30 |
| `network.resolvers` | `["9.9.9.9"]` | one to four IPv4 addresses, none in a protected network |
| `limits.cpus` | `2.0` | 0.1 to 16.0 |
| `limits.memory_mib` | `1536` | 128 to 65536 |
| `limits.pids` | `256` | 32 to 4096 |
| `limits.guard_memory_mib` | `32` | 16 to 512 |
| `limits.turn_seconds` | `7200` | 60 to 86400 |
| `limits.silence_seconds` | `600` | 30 to 86400 |
| `limits.refusals_per_minute` | `120` | 10 to 100000 |
| `tokens.lifetime_seconds` | `14400` | 120 to 604800 |
| `tokens.rotate_before_seconds` | `900` | 30 to 86400 |
| `gateway.provider` | `"stub"` | `stub` or `openai-compatible` |
| `gateway.upstream_url` | unset | an http or https URL; required with `openai-compatible` |
| `gateway.credential_file` | unset | an absolute path; required with `openai-compatible` |
| `gateway.model` | `"zephyros-default"` | a name |
| `gateway.upstream_model` | unset | a model name of the provider |
| `gateway.models` | unset | one to eight model names; required with `openai-compatible` |
| `gateway.session_header` | unset | a header name |
| `gateway.timeout_seconds` | `600` | 10 to 3600 |
| `gateway.simulation` | `false` | `true` or `false` |
| `forge.provider` | `"none"` | `none`, `directory` or `github` |
| `forge.root` | `"/var/lib/zephyros/forge"` | an absolute path |
| `forge.token_file` | unset | an absolute path; required with `github` |
| `verification.runner` | `"none"` | `none` or `sandbox` |
| `verification.kit` | `"tests"` | a name |
| `verification.kit_image` | unset | an image, by digest or local identifier; required with `sandbox` |
| `verification.timeout_seconds` | `300` | 30 to 3600 |
| `verification.protected_paths` | `["CONTRACT.md"]` | one to 32 relative paths; a directory ends with `/` |
| `notifications.url` | `""` | an http or https URL, or empty |
| `notifications.secret` | `""` | 16 to 512 characters when the URL is set, or empty |
| `continuity.inactivity_seconds` | `900` | 60 to 86400 |
| `continuity.restarts` | `2` | 1 to 10 |
| `continuity.unblock_seconds` | `3600` | 60 to 86400 |
| `continuity.rejections` | `3` | 1 to 50 |

## Versions

1.1.0 added `runtime.guard_health_seconds`, `runtime.poll_seconds` and `limits.silence_seconds`; 1.2.0 added `[forge]` and `[verification]`, and local image identifiers; 1.3.0 added `network.resolvers` (default `9.9.9.9`, Quad9); 1.4.0 added `[notifications]` and the signed webhooks (EXG-43); 1.5.0 added `[continuity]` (US-E7-01, EXG-23); 1.6.0 added `limits.refusals_per_minute` (ATK-71); 1.7.0 added `gateway.simulation` (EXG-46); 1.8.0 added `gateway.models`, checked against a list of models held in the code; 1.9.0 added the GitHub forge (`forge.provider` `github` and `forge.token_file`); 1.10.0 added `gateway.session_header`; 1.11.0 added `gateway.timeout_seconds`; 1.12.0 added `verification.protected_paths` and raised the defaults of `limits.memory_mib` (1536) and `limits.turn_seconds` (7200) to the measures of the second real run (ADR 0020); 1.13.0 removed the list of models from the code: `gateway.models` alone says which models the gateway may serve.

Each addition has a default, except one: since 1.8.0, an `openai-compatible` provider needs `gateway.models`, so a file older than 1.8.0 that uses one must add it. Any other 1.x file still reads.
