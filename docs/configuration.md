# Configuration

The hub reads one TOML file. It validates the file against a versioned schema and **refuses to start** when the file violates it, naming each faulty key and saying why.

```bash
zephyros config check zephyros.toml     # validate a file
zephyros config schema                  # print the JSON Schema
```

The schema is published as [`schemas/configuration.schema.json`](../schemas/configuration.schema.json), version 1.18.0. A file declares the version it was written for in `schema_version`; the hub reads a file of any 1.x version, save the few values a later version refuses (see [Versions](#versions)), and refuses another major version. Unknown keys are refused.

A minimal file:

```toml
schema_version = "1.18.0"

[images]
instance = "registry.example/zephyros-instance@sha256:…"   # by digest, always
guard = "sha256:…"   # or a local image by its identifier
```

On one machine, `scripts/deploy.sh up` writes the file (`deploy/state/zephyros.toml`) with the version the code speaks and the images it built, then appends `ZEPHYROS_EXTRA_CONFIG` ([running Zephyros](running.md), [a run with real models](real-test.md)).

## Sections

| Section | What it holds |
| --- | --- |
| `[hub]` | The listeners: the agent endpoint (the hub destination open to every instance), the egress proxy (open to a `regulated` instance only), the operator channel and the main-agent endpoint. The last two never listen on an instance network: a wildcard address and an address of the instance pool are refused, and `"core"` (what `scripts/deploy.sh` writes) makes the hub in its container listen on its address of the core network, the one its ports are published on, and on its own loopback. An instance reaches neither, whether its guard runs or not ([ADR 0025](decisions/0025-operator-and-main-agent-off-the-instance-networks.md)) |
| `[paths]` | The data directory, the Unix socket of the container filter, the files that hold the operator and main-agent tokens, and the control socket on which `zephyros hub rotate-token` asks the hub for a new one |
| `[runtime]` | The name of the core network and of the hub container; how long the hub waits for a network guard to report healthy, and how often it looks at its instances |
| `[images]` | The instance image and the network guard image, by registry digest (`name@sha256:…`) or by local identifier (`sha256:…`) (required); the image of an instance that runs OpenCode (`opencode`), when one does |
| `[instances]` | The agent engine of the Contractor instance of each project: `hermes` (the default) or `opencode`, a third-party coding agent (see below). The Owner instance runs Hermes |
| `[network]` | The range the subnets of the instance networks are taken from, their prefix length, and the name resolvers an `open` instance uses (one to four public addresses; the default is `9.9.9.9`, Quad9; a `regulated` or `sealed` instance sends no query: the egress proxy resolves for it). A resolver inside a protected network is refused at startup, because the network guard blocks it ([ADR 0018](decisions/0018-instance-name-resolution.md)). What an instance reaches besides the hub (`egress`, see below) and the hosts a `regulated` instance reaches (`allowed_hosts`) |
| `[limits]` | CPUs, memory and process count of an agent container, memory of a guard, the longest duration of a turn, how long a turn may go without calling the hub before it is stopped, and how many refused actions of one instance in a minute make the hub answer without doing the work (`refusals_per_minute`, ATK-71) |
| `[tokens]` | The lifetime of a project token and how long before its expiry a new turn gets a new one; the lifetime of the operator and main-agent tokens (`channel_lifetime_days`, 90 days by default, see below) |
| `[forge]` | Where pull requests are opened: `none`, `directory` (bare git repositories in a directory, for the development deployment and the tests), or `github` (the repositories of the person, through the API and git over https, with the file of its token in `token_file`; read at each call) |
| `[verification]` | What runs the acceptance tests of a delivery (`none`, which ends every verification in an error, or `sandbox`), the verification kit and its image, the longest run (up to 24 hours, see [the sandbox](#the-sandbox-of-verification)), and the client's own files (`protected_paths`, `CONTRACT.md` by default): a delivery that changes one is flagged in its report and its pull request, never refused ([ADR 0020](decisions/0020-the-owner-sees-and-records.md)) |
| `[gateway]` | The model provider behind the gateway (`stub`, or `openai-compatible` with a URL and a credential file), the model name instances ask for, the name of the model at the provider when it differs, the models the gateway may serve (`models`, see below), `simulation` to mark the report, the pull request and the events of a run with the stub (EXG-46), the name of the session header the gateway sends to the provider (`session_header`, for OpenCode Go's `x-opencode-session`; one stable session name per hub process), how long the gateway waits for one answer of the provider (`timeout_seconds`: a long answer of a real model takes minutes), whether the reasoning of the model goes back to the provider with the conversation (`reasoning_echo`, see below), and the context window of the model with the context the agents work with (`context_window`, `context_tokens`, see below). The credential file is read at each call, so changing it takes effect on the next call |
| `[tools]` | The tools of the projects: a directory of the machine mounted read-only at `/opt/project-tools` in every turn and every sandbox (see [the tools of a project](#the-tools-of-a-project)) |
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

## The reasoning of the model

A thinking model gives its reasoning with each answer (`reasoning_content`, beside the content and the tool calls), and providers disagree on what to do with it next. DeepSeek in thinking mode wants it back: a conversation whose assistant message with tool calls comes without its reasoning is refused (HTTP 400, "The `reasoning_content` in the thinking mode must be passed back to the API"). A strict OpenAI-compatible provider refuses the field on any message, even empty. `gateway.reasoning_echo` says which kind the provider is ([ADR 0032](decisions/0032-the-gateway-carries-the-reasoning-of-the-model.md)):

- `true`: the gateway sends every assistant message to the provider with its reasoning, and a message that has none (an answer that came without reasoning, or an engine that dropped it) with a single space, which DeepSeek accepts where it refuses an empty text. Each turn tells its engine to keep the reasoning in the conversation it sends again: `model.reasoning_echo` for Hermes, the `interleaved` field of the model for OpenCode, whatever the model is named behind the gateway.
- `false`: no message goes to the provider with the field, and the engines keep no reasoning.
- Unset: `true` when the model at the provider (`gateway.upstream_model`, or `gateway.model` when that is unset) holds `deepseek`, `kimi` or `mimo`, or the provider's host is that of DeepSeek, Kimi (Moonshot) or MiMo; `false` otherwise, and always `false` with the stub. The name instances ask for (`gateway.model`, `"served-model"` in these examples) does not count when `upstream_model` is set.

The gateway always gives the instance the reasoning of an answer, whole or streamed. Set the key when the provider is one the rule does not know: a DeepSeek model served under another name wants `true`; a DeepSeek or Kimi model at a strict host (some resellers of open models; Hermes itself recognises Kimi by its host alone for that reason) wants `false`.

## The context of the agents

The agents of both sides work with the whole context window of the model ([ADR 0033](decisions/0033-the-agents-work-with-the-context-of-the-model.md)). Neither engine can know it from the name the model has behind the gateway, so the operator declares it:

```toml
[gateway]
context_window = 1000000   # the window of the model, as its provider gives it
context_tokens = 400000    # optional: less of it for the agents (400 000 or 256 000, never below)
```

`context_window` is 256 000 when unset, and 64 000 at least (the smallest window the pinned Hermes Agent starts with). `context_tokens` is the whole window when unset, and never more than the window. Both engines compact the conversation at the same point: `context_tokens`, or the window less a margin of a tenth of it (32 000 tokens at least) when that comes first. The margin keeps room for the answer of the model and the results of its tools, so that the provider never refuses a call for its length. Hermes takes the window as `model.context_length` and the point as `compression.threshold_tokens`; OpenCode takes the window as the context of its model, and an input limit 16 000 tokens above the point (the answer it reserves). `zephyros config check` prints the three figures.

**Below 256 000 tokens, the hub warns.** A compaction point below 256 000 tokens, whether a smaller `context_tokens` or a model whose window leaves no more, is accepted. So is a real model whose window the file does not declare (the window is then a guess of 256 000, and the agents compact at 224 000). Both are said where they cannot be missed: by `zephyros config check`, which `scripts/deploy.sh up` runs before it starts anything; on the standard error of the hub and in its log at every start (`context.check`, with `below_floor`, `context_declared` and the warnings); and, for the floor, at the top of the evidence report of each delivery whose turns compacted below it (CONTEXT BELOW THE FLOOR). Below 256 000 tokens, a long task loses the thread of its work: compactions repeat and the agent re-reads what it already knew. With the stub there is no model, and nothing is said. Each start of a turn records the figures (`turn.start`: `context_window`, `context_tokens`, `compaction_tokens`).

**How large a conversation the gateway takes.** The gateway takes a request of 8 bytes a token of the window, plus 1 MiB for the system prompt and the definitions of the tools. That is twice the 3.9 bytes a token measured on the JSON of a real conversation of 152 calls; the 25 tools of Hermes and its system prompt measured 55 000 bytes. It also takes one message per 64 tokens of the window (1 000 messages at least). A conversation at the compaction point passes, and keeps room for what a turn adds before the engine compacts. Whatever the window, a request stays below 16 MiB: parsing one takes about twice its size, and the hub (512 MiB) may serve several instances at once. That bound covers a window of about two million tokens; past it, the gateway refuses a larger request (413), and the operator should give the agents less with `context_tokens`.

**Cost.** Each call sends the whole conversation, so a long context means more input tokens a call. A provider with a prompt cache bills the part of the conversation it has already seen at a fraction of the price. DeepSeek does, for example. A turn that appends to its conversation is mostly that part. Compaction breaks the cache: the summary is new text, billed in full, and so is everything after it, and writing it costs calls of its own. The evidence report and `gateway.call` give the tokens used.

**Latency.** A call with a long conversation takes longer before its first token, more so when the cache misses. `gateway.timeout_seconds` bounds one answer and `limits.turn_seconds` a turn; raise them if long answers come close.

Declare the window the provider gives, not more: past the real window, the provider refuses the calls, with a reason the log of the hub keeps (`gateway.call`).

## The agent of the Contractor

`instances.contractor.engine` chooses the coding agent that runs in the Contractor instance of every project ([ADR 0029](decisions/0029-the-contractor-can-be-a-third-party-coding-agent.md)): `hermes`, the default, in `images.instance`, or `opencode`, OpenCode 1.19.0, in `images.opencode` (built from `images/instance-opencode`; `scripts/deploy.sh` builds it when `ZEPHYROS_CONTRACTOR_ENGINE=opencode`). The Owner instance runs Hermes.

```toml
[images]
instance = "sha256:…"
guard = "sha256:…"
opencode = "sha256:…"

[instances.contractor]
engine = "opencode"
```

What holds does not depend on the choice: the agent reaches the model through the gateway of the hub alone, with the project token, and never sees the provider key; it acts on the hub through the same tools and rights, so it can neither write the contract nor change a test; and it runs under the same container template, network guard and level, sandbox and gate. What differs is the image, the command of a turn (`opencode run`, continuing the one session of the instance from one turn to the next), how the agent compacts its context, and what it keeps on its home volume. A change of engine takes effect at the next turn; the home volume keeps what the former engine wrote there, and the new one ignores it. Each start of a turn records the engine and its version in the log, and the evidence report of a delivery names the engines of the Contractor turns before it.

OpenCode has been run only with the obedient stub, a scripted model, not yet with a real one: how a real model works through it, and what a turn costs, remain to be measured. Known limits of OpenCode 1.19.0 here: it does not read the `AGENTS.md` of the work directory as instructions (the agent can still read the file); it asks the model for the title of a new session, one more counted call; and it retries a failed model call for about a minute before the turn ends with an error.

## What an instance reaches

`network.egress` sets the network access of every instance, Owner and Contractor alike ([ADR 0024](decisions/0024-three-levels-of-network-access.md)). No level reaches a protected network (the private ranges, loopback, link-local, the shared address space, cloud metadata), and a sandbox of verification has no network at any level.

| Level | What an instance reaches | When |
| --- | --- | --- |
| `regulated` (default) | The hub, so the model through its gateway, and the hosts of `network.allowed_hosts` on port 443 through the egress proxy of the hub. Everything else is refused. The hub logs every destination asked for, allowed or refused (`instance.egress`) | Normal use |
| `sealed` | The hub alone. What the work needs is in the instance image (Python with pytest, see [the tools of an instance](instance-tools.md)) | A benchmark, a sensitive project |
| `open` | Every public destination, as before 1.14.0 | An explicit exception: the hub logs a warning (`instance.egress`) each time it starts an instance |

The default list holds the package registries: `pypi.org`, `files.pythonhosted.org` and `registry.npmjs.org`. GitHub is not in it. To add a host, write the whole list:

```toml
[network]
egress = "regulated"
allowed_hosts = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com"]
```

A host is a name, in lower case, with no port and no wildcard; each subdomain is named on its own line. An address is refused: the proxy allows a name, then refuses it when it resolves into a protected network. For no host at all, choose `sealed`. A regulated instance gets the proxy in `HTTPS_PROXY`, which `pip`, `npm`, `uv`, `curl` and most HTTP clients follow; a client that ignores it reaches nothing.

The proxy opens a tunnel to an allowed host whatever the request inside: it does not make a registry read-only (ADR 0024, consequences).

**Changing the level.** A guard loads its rules when it starts and carries its level as a label. When the hub starts, and each time it looks at an instance, it takes down a guard whose level is not the configured one (an instance made before 1.14.0 included) and logs it (`instance.egress`, `why: level_changed`). The turn under way stops; the volumes stay, and the next start of the instance brings it up at the new level.

With `scripts/deploy.sh`, `ZEPHYROS_EGRESS` sets the level and `ZEPHYROS_ALLOWED_HOSTS` the list (comma-separated).

## The tools of a project

Some projects need heavy, fixed tooling that neither side may fetch or change: a simulator, a headless engine, a test bench and its scripts. `tools.source` names a directory of the machine that holds it ([ADR 0030](decisions/0030-the-tools-of-a-project-are-mounted-read-only.md)):

```toml
[tools]
source = "/srv/zephyros-tools"
```

With `scripts/deploy.sh`, `ZEPHYROS_TOOLS=/srv/zephyros-tools` writes that table.

The container filter mounts the directory **read-only at `/opt/project-tools`** in every turn, Owner and Contractor, whatever the engine, and in every sandbox of verification: the acceptance tests run with the tools the agents had. `/opt/project-tools/bin` is the last directory of the `PATH` in the three images, so a tool there runs by its name (`sim`), and never hides a program of the image. The mount is not recursive: a file system mounted under the directory on the machine would show there as an empty directory, so the hub refuses one (below). The directory is the same for every project of the deployment.

The hub reads the directory at that same path (`deploy/compose.yaml` mounts it into the hub container there, read-only and not recursive), and **refuses to start** when the user of the instances (uid and gid 10000) could change it, or when it holds a link that leads out of it. Each path at fault is named:

```text
refused to start: the tools directory '/srv/zephyros-tools' cannot be mounted:
  'bin/sim' is writable by every user
  'lib/libsim.so' is a link that leads out of the tools directory
```

What is refused:

- an entry owned by uid 10000, writable by gid 10000 or by every user, or carrying a POSIX access control list;
- a link to an absolute path, or one that leads out of the directory: on the machine, or on its way, read step by step as the kernel reads it in a container. There the directory is `/opt/project-tools`, so a `..` above it is a way out, even when the next names lead back in on the machine: in `/srv/sim/build`, the link `lib/cur -> ../../build/lib` resolves to `/srv/sim/build/lib` on the machine, but to `/opt/build/lib` in a turn, and is refused. A loop of links is refused too;
- a file system mounted under the directory, when the hub sees it: the turns and the sandbox would find an empty directory there. The mount of the hub is not recursive, so it sees what they see, but some versions of Compose (2.26 among them) drop `recursive: disabled` without a word: the hub then sees the file system, and refuses rather than fingerprint files the turns do not have. `scripts/deploy.sh` checks with a recursive mount, so it refuses one in every case;
- a device, a socket or a pipe; a setuid or setgid file; a file the hub (uid 10001) cannot read.

A directory owned by its operator with modes `0755` and `0644` (and `0755` for programs) passes. `scripts/deploy.sh` runs the same check before it writes the configuration or starts anything. The hub checks again at each start of a turn and at each verification: a turn whose tools have since become refused does not start (`turn.start`, `error: tools_refused`, with the problems), and such a verification ends in an error. The log of a project and the status of a delivery, which the agents read, name the paths inside the directory only, never its path on the machine; that path is in the log of the hub (`tools.check`: `source`) and in the message of a hub that refuses to start.

**The fingerprint.** The hub fingerprints the directory when it starts (`tools.check`, in the log of the hub), at each start of a turn (`turn.start`: `tools`), and before and after each verification (`verification.run`: `tools`); when the two around a verification differ, its results are dropped and the delivery ends in an error. The evidence report gives it, with the number of files and their size, and flags the turns of either side, before the delivery, that found other tools when they started (read from the first 100 000 records of the log of the project, as the cost is). These are measures at given moments, not a watch: a change undone before the end of a verification, or made during a turn, is not seen. To check a copy of the tools against a report, anywhere:

```bash
zephyros tools check /path/to/tools
# {"tools": "sha256:…", "files": 12, "bytes": 314572800}
```

It is the SHA-256 of one JSON line per entry, sorted by path (relative, `/` between names, the directory itself `""`): `["d", path]` for a directory, `["f", path, "x" or "-", size, sha256]` for a file (`x` when any execute bit is set), `["l", path, target]` for a link. Owners, times and other mode bits do not count.

The hub walks the whole tree at each measure, and hashes the content again only when the status of an entry changed (size, times, inode, mode, owner). A change of content moves the change time (ctime) of the file, which no program sets at will; since the kernel moves it by ticks of a few milliseconds, a result is kept only once every entry is at least two seconds old.

**What a tool needs to run everywhere.** The three images do not share a C library: the Hermes image is Debian 13 (glibc 2.41), the OpenCode image and the `tests` kit are Alpine 3.24 (musl). A program linked against the glibc of the machine runs in a Hermes turn and fails at the gate with `not found`. Give the tools what they run on:

- a statically linked program runs in all three;
- a dynamically linked one runs through the loader and the libraries the directory carries, from a small script:

  ```sh
  #!/bin/sh
  # bin/sim: the simulator, with the glibc it was built against (lib/ holds ld-linux and the libraries ldd lists)
  here=$(dirname "$(readlink -f "$0")")/..
  exec "$here/lib/ld-linux-x86-64.so.2" --library-path "$here/lib" "$here/libexec/sim" "$@"
  ```

- a Python script runs on the Python of the image, 3.14 in a Hermes turn and 3.12 in the OpenCode image and at the gate; one that needs a package brings it in the directory (on its `PYTHONPATH`, set by the script that starts it).

The tools are the operator's, and trusted: Zephyros keeps everyone inside it from changing them, not from reading them. Both sides read them, so they hold no secret and no hidden part of an evaluation, which belongs to the acceptance tests of the Owner side.

**A turn that runs a tool for long.** The hub stops a turn that has called neither the hub nor the model for `limits.silence_seconds` (600 s by default): the turn ends `silent`. A turn that runs a simulation of more than ten minutes and waits for it says nothing meanwhile, and is stopped. When a tool runs long in a turn, set the silence above its longest run, with room to spare, and `limits.turn_seconds` above the whole turn; the sandbox of verification has no such limit, only `verification.timeout_seconds`:

```toml
[limits]
silence_seconds = 3600   # a run of the simulator takes up to 45 min
turn_seconds = 14400
```

**What has not been checked.** The check judges the owners and the modes the hub sees, which are those of the instances only when the runtime maps no user: with Docker in rootless mode or with user namespace remapping (`userns-remap`), uid 10000 in a container is another uid on the machine, and the check does not know which; neither the check nor the runtime tests have run there. It reads POSIX access control lists only: an NFSv4 one (`system.nfs4_acl`) is not seen. It has been run with Docker on Linux only: on macOS the owner shown for a directory shared from the host may not be the owner on the host, and whether Podman honours a non-recursive bind has not been checked.

## The sandbox of verification

A sandbox runs the acceptance tests of one delivery, as user 10000, with no network, a read-only root, the files to verify in a volume of its own on `/work`, and the tools of the projects at `/opt/project-tools`. Its resources are those of a turn:

| What | Default | Key |
| --- | --- | --- |
| Processors | 2 | `limits.cpus` (0.1 to 16) |
| Memory, with no swap | 1 536 MiB | `limits.memory_mib` (128 to 65 536) |
| Processes and threads | 256 | `limits.pids` (32 to 4 096) |
| `/tmp` | 64 MiB in memory, counted in the memory above, where no program runs (`noexec`) | fixed |
| `/work` | the files of the delivery, on a volume of the runtime: writable, and a program there runs | fixed |
| Longest run | 300 s | `verification.timeout_seconds` (30 to 86 400) |

A tool that unpacks a program to run it unpacks it in `/work`. Raising `limits.*` raises them for the turns too. A verification that reaches `verification.timeout_seconds` is stopped and verifies nothing: every test reads `not_run`.

The delivery loop runs one job at a time, for every project, on one thread of the hub: a verification, and also the preparation of a project's base from the forge and the comments and titles of its pull requests. A verification of two hours makes every one of them wait, for every project; the turns do not wait (their watch runs on another thread). The queue of the loop is in memory: when the hub starts again, it queues again, from the start, the verification of every delivery declared and not decided. A sandbox lives inside one call of the container filter, which removes it at the end whatever happens: a sandbox whose hub stopped runs on until it ends or reaches `verification.timeout_seconds`, then is removed, its result lost; one whose filter stopped is removed when the filter starts again.

For tests that play several runs of a few minutes, set the limit to cover them all with room to spare:

```toml
[verification]
timeout_seconds = 9000   # 2 h 30
```

## Where the operator channel and the main-agent endpoint listen

With `"core"` (what `scripts/deploy.sh` writes), the hub reads the IPv4 addresses of its container when it starts, and listens on the one that is neither a loopback nor inside `network.instance_pool` (its address on the core network), and on its own loopback ([ADR 0025](decisions/0025-operator-and-main-agent-off-the-instance-networks.md)). When there is not exactly one such address, it refuses to start, before anything opens, and names what it found:

```
refused to start: "core" needs exactly one address of the hub outside the instance pool 10.200.0.0/16 and the loopback; found none
```

- **found none**: the instance pool covers the core network too, for example a `ZEPHYROS_POOL` of `172.16.0.0/12` where the runtime gives the core network an address in `172.18.0.0/16`. Choose a pool that no other network of the machine uses (the default, `10.200.0.0/16`, or another range of `10.0.0.0/8`), then `scripts/deploy.sh up` again.
- **found two or more**: the hub is still attached to instance networks taken from a former pool, outside the current one. `scripts/deploy.sh down` removes every instance network (the instances keep their volumes), and `scripts/deploy.sh up` starts the hub again; the hub creates the networks again from the current pool when it starts the instances.

## The operator and main-agent tokens

Both tokens hold every right of their channel, so they expire: `tokens.channel_lifetime_days` after they are issued, 90 days by default ([ADR 0026](decisions/0026-a-lifetime-and-a-rotation-for-the-operator-and-main-agent-tokens.md)). The hub writes each one to its file (`paths.operator_token_file`, `paths.main_agent_token_file`, mode 0600) when it first starts, and again at a start that finds no working token in the file. A token issued before 1.15.0 never expired: the first start of the hub after the update gives it the configured lifetime, counted from that start, and logs it (`token.lifetime`).

To rotate a token, before it expires or once it has:

```bash
scripts/deploy.sh rotate-token operator     # or: main-agent
```

It runs `zephyros hub rotate-token` in the hub container, which asks the running hub on its control socket (`paths.control_socket`, open to the user of the hub alone): the hub issues a new token, writes it to the file (mode 0600), then revokes every other token of that kind, and logs both (`token.issue` and `token.revoke`, `why: rotation`). The answer names the file and the new expiry, never the token. `scripts/deploy.sh call` reads the operator token from its file at each call, so nothing else changes for the operator; the main agent must be given the new main-agent token. Running instances and their turns are not touched.

An expired token is refused (`authentication`, reason `expired`, in the log), and the answer says what to do:

```json
{"error": "token_expired", "message": "the operator token expired at 2027-01-12T10:00:00.000000Z; on the machine of the hub, run `scripts/deploy.sh rotate-token operator`: ..."}
```

## Rules across keys

The schema document lists them (`x-zephyros-rules`):

- the four ports of `[hub]` differ;
- `hub.operator_bind` and `hub.main_agent_bind` are not addresses of `network.instance_pool`;
- a project token outlives the longest turn, and its rotation starts before its expiry;
- the subnet prefix of an instance network is longer than the prefix of the pool;
- no name resolver is in a protected network;
- a `github` forge has its `token_file`;
- an `openai-compatible` provider has its `upstream_url` and its `credential_file`, and the model it sends is one of `gateway.models`;
- a `sandbox` runner has its `kit_image`;
- a Contractor that runs `opencode` has its image, `images.opencode`;
- `gateway.simulation` runs only with the `stub` provider;
- `gateway.context_tokens` is at most `gateway.context_window`;
- the notifications have both their URL (http or https) and their secret (16 characters at least), or neither.

## Every key and its default

A key that a file leaves out takes its default. "Unset" means that the key has no default and that nothing uses it until the file sets it.

| Key | Default | Values |
| --- | --- | --- |
| `schema_version` | required | a version, `1.x.y` |
| `hub.agent_port` | `8080` | 1024 to 65535 |
| `hub.operator_port` | `8081` | 1024 to 65535 |
| `hub.main_agent_port` | `8082` | 1024 to 65535 |
| `hub.operator_bind` | `"127.0.0.1"` | an IP address that is neither a wildcard nor in the instance pool, or `"core"` |
| `hub.main_agent_bind` | `"127.0.0.1"` | an IP address that is neither a wildcard nor in the instance pool, or `"core"` |
| `hub.egress_port` | `8083` | 1024 to 65535 |
| `paths.data_dir` | `"/var/lib/zephyros"` | an absolute path |
| `paths.filter_socket` | `"/run/zephyros/filter.sock"` | an absolute path |
| `paths.operator_token_file` | `"/var/lib/zephyros/operator.token"` | an absolute path |
| `paths.main_agent_token_file` | `"/var/lib/zephyros/main-agent.token"` | an absolute path |
| `paths.control_socket` | `"/var/lib/zephyros/control.sock"` | an absolute path, 100 characters at most |
| `runtime.core_network` | `"zephyros-core"` | a name |
| `runtime.hub_container` | `"zephyros-hub"` | a name |
| `runtime.guard_health_seconds` | `60` | 5 to 600 |
| `runtime.poll_seconds` | `5` | 1 to 60 |
| `images.instance` | required | an image, by digest or local identifier |
| `images.guard` | required | an image, by digest or local identifier |
| `images.opencode` | unset | an image, by digest or local identifier; required when `instances.contractor.engine` is `opencode` |
| `instances.contractor.engine` | `"hermes"` | `hermes` or `opencode` |
| `network.instance_pool` | `"10.200.0.0/16"` | an IPv4 network |
| `network.instance_prefix` | `28` | 24 to 30 |
| `network.resolvers` | `["9.9.9.9"]` | one to four IPv4 addresses, none in a protected network; used by `open` |
| `network.egress` | `"regulated"` | `regulated`, `sealed` or `open` |
| `network.allowed_hosts` | `["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]` | one to 64 host names, lower case, no address and no wildcard; used by `regulated` |
| `limits.cpus` | `2.0` | 0.1 to 16.0 |
| `limits.memory_mib` | `1536` | 128 to 65536 |
| `limits.pids` | `256` | 32 to 4096 |
| `limits.guard_memory_mib` | `32` | 16 to 512 |
| `limits.turn_seconds` | `7200` | 60 to 86400 |
| `limits.silence_seconds` | `600` | 30 to 86400 |
| `limits.refusals_per_minute` | `120` | 10 to 100000 |
| `tokens.lifetime_seconds` | `14400` | 120 to 604800 |
| `tokens.rotate_before_seconds` | `900` | 30 to 86400 |
| `tokens.channel_lifetime_days` | `90` | 1 to 3650 |
| `gateway.provider` | `"stub"` | `stub` or `openai-compatible` |
| `gateway.upstream_url` | unset | an http or https URL; required with `openai-compatible` |
| `gateway.credential_file` | unset | an absolute path; required with `openai-compatible` |
| `gateway.model` | `"zephyros-default"` | a name |
| `gateway.upstream_model` | unset | a model name of the provider |
| `gateway.models` | unset | one to eight model names; required with `openai-compatible` |
| `gateway.session_header` | unset | a header name |
| `gateway.reasoning_echo` | unset | `true` or `false`; unset follows the model at the provider and its host (see [the reasoning of the model](#the-reasoning-of-the-model)) |
| `gateway.context_window` | `256000` | 64000 to 10000000 tokens: the window of the model (see [the context of the agents](#the-context-of-the-agents)); with a real model, the hub warns when it is not declared; the gateway takes conversations as large as it, within 16 MiB |
| `gateway.context_tokens` | unset | 32000 to 10000000 tokens, at most `gateway.context_window`; unset is the whole window; a compaction point below 256 000 is accepted with a warning |
| `gateway.timeout_seconds` | `600` | 10 to 3600 |
| `gateway.simulation` | `false` | `true` or `false` |
| `forge.provider` | `"none"` | `none`, `directory` or `github` |
| `forge.root` | `"/var/lib/zephyros/forge"` | an absolute path |
| `forge.token_file` | unset | an absolute path; required with `github` |
| `verification.runner` | `"none"` | `none` or `sandbox` |
| `verification.kit` | `"tests"` | a name |
| `verification.kit_image` | unset | an image, by digest or local identifier; required with `sandbox` |
| `verification.timeout_seconds` | `300` | 30 to 86400 |
| `verification.protected_paths` | `["CONTRACT.md"]` | one to 32 relative paths; a directory ends with `/` |
| `tools.source` | unset | the absolute path of a directory of the machine, not `/`, with no `.` or `..` step, no trailing `/`, and letters, digits and `._@+-` between the `/`; checked when the hub starts |
| `notifications.url` | `""` | an http or https URL, or empty |
| `notifications.secret` | `""` | 16 to 512 characters when the URL is set, or empty |
| `continuity.inactivity_seconds` | `900` | 60 to 86400 |
| `continuity.restarts` | `2` | 1 to 10 |
| `continuity.unblock_seconds` | `3600` | 60 to 86400 |
| `continuity.rejections` | `3` | 1 to 50 |

## Versions

1.1.0 added `runtime.guard_health_seconds`, `runtime.poll_seconds` and `limits.silence_seconds`; 1.2.0 added `[forge]` and `[verification]`, and local image identifiers; 1.3.0 added `network.resolvers` (default `9.9.9.9`, Quad9); 1.4.0 added `[notifications]` and the signed webhooks (EXG-43); 1.5.0 added `[continuity]` (US-E7-01, EXG-23); 1.6.0 added `limits.refusals_per_minute` (ATK-71); 1.7.0 added `gateway.simulation` (EXG-46); 1.8.0 added `gateway.models`, checked against a list of models held in the code; 1.9.0 added the GitHub forge (`forge.provider` `github` and `forge.token_file`); 1.10.0 added `gateway.session_header`; 1.11.0 added `gateway.timeout_seconds`; 1.12.0 added `verification.protected_paths` and raised the defaults of `limits.memory_mib` (1536) and `limits.turn_seconds` (7200) to the measures of the second real run (ADR 0020); 1.13.0 removed the list of models from the code: `gateway.models` alone says which models the gateway may serve; 1.14.0 added `network.egress`, `network.allowed_hosts` and `hub.egress_port`, and made `regulated` the default: an instance no longer reaches the whole Internet unless the file says `open` (ADR 0024); 1.15.0 refused a wildcard or an instance-pool address in `hub.operator_bind` and `hub.main_agent_bind` and added the value `"core"` (ADR 0025), and added `tokens.channel_lifetime_days` and `paths.control_socket` (ADR 0026); 1.16.0 added `images.opencode` and `instances.contractor.engine` (ADR 0029); 1.17.0 added `[tools]` and raised the bound of `verification.timeout_seconds` from 3 600 to 86 400 (ADR 0030); 1.18.0 added `gateway.reasoning_echo` (ADR 0032), `gateway.context_window` and `gateway.context_tokens` (ADR 0033).

Each addition has a default, except one: since 1.8.0, an `openai-compatible` provider needs `gateway.models`, so a file older than 1.8.0 that uses one must add it. Since 1.15.0, a file that binds the operator channel or the main-agent endpoint to `0.0.0.0` is refused: write `"core"` for a hub in a container. Any other 1.x file still reads.
