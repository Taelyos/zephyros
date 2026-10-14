# Running Zephyros with real models

With the default configuration, the **simulated** model provider answers the instances: the
obedient stub runs the whole delivery loop and nothing spends. A run with real models is the same
loop with real agents behind the gateway. Zephyros has had three such runs, all with DeepSeek V4.1
Flash on every side, through OpenCode Go, on small Python projects (see the
[README](../README.md)). This page is how to run one.

## What changes: the gateway

The hub never gives an instance a provider credential (EXG-04,
[ADR 0008](decisions/0008-model-gateway-in-hub.md)): it is a gateway. The instances run Hermes,
which asks the gateway; the gateway forwards each call to an OpenAI-compatible provider, with the
credential file the configuration names. Swapping the provider is one table of the configuration:

```toml
[gateway]
provider = "openai-compatible"
upstream_url = "https://provider.example/v1"
credential_file = "/run/zephyros-credentials/gateway.key"
model = "served-model"                 # the name the instances ask for
upstream_model = "deepseek-v4.1-flash" # what the gateway sends to the provider
models = ["deepseek-v4.1-flash"]       # the models the gateway may send: this one alone
session_header = "x-opencode-session"
simulation = false
```

`session_header` names the session header the provider asks for — OpenCode Go refuses a call
without `x-opencode-session` (HTTP 400), and the gateway sends one stable session name per
process. A provider that asks for none: leave it unset. `timeout_seconds` (600 by default) is how
long the gateway waits for one answer: a long answer of a real model takes minutes.

Rules the schema enforces: `upstream_url` and `credential_file` are set; the model the gateway
sends is one of `gateway.models`; `simulation = true` runs only with the stub (a real provider is
not a simulation); the credential file is read at every call, so rotating it takes effect on the
next call.

### One model, and no other

`gateway.models` is the whole model policy: the code authorises no model of its own. The table
above authorises DeepSeek V4.1 Flash alone, as the three real runs did, and the hub refuses to
start with any other `upstream_model`. `scripts/deploy.sh up` checks the file before it starts
anything:

```text
gateway.upstream_model: 'claude-opus-5-5' is not one of gateway.models (deepseek-v4.1-flash): the gateway serves only the models its configuration names
```

The policy is only as safe as the file that holds it. Keep the table in a file of your own,
outside the repository — beside the key, readable by you alone — and pass it to `deploy.sh` in
`ZEPHYROS_EXTRA_CONFIG`. To authorise another model, change that file: nothing in the code has to
change.

### Where the key file lives

The hub runs in a container: the key is not copied into it. The file stays where it is on the
host and `scripts/deploy.sh` bind-mounts it, read-only, at
`/run/zephyros-credentials/gateway.key` — the path the configuration names. The hub runs as uid
10001, so the file must be readable by that uid: `deploy.sh` gives it exactly that one read ACL,
`setfacl -m u:10001:r <the file>`, rather than opening it to everyone. On a host without
`setfacl` it prints a warning and goes on; make the file readable by uid 10001 another way for the
run, and restore its mode afterwards. The same holds for the token of a GitHub forge, mounted at
`/run/zephyros-credentials/forge.key`. With no key given, `deploy.sh` mounts an empty file
instead, so the variables always resolve.

## How to run it

1. **Prepare, outside the repository.** The provider key in its file; the `[gateway]` table in
   another file; for a GitHub forge, the project's repository on GitHub and a token file that can
   push a branch and open a pull request on it. Write the contract of the project as Markdown
   (requirement and constraint tables, story sections with their criteria), and its acceptance
   tests — or let the Owner instance write the stories, criteria and tests (step 4).

   The `tests` kit runs acceptance tests as **black box** ([ADR 0017](decisions/0017-verification-runner-and-kits.md)):
   a test runs the delivery as a subprocess and reads its output
   (`subprocess.run([sys.executable, "-m", "calc", "1", "2"], ...)`, or
   `python -c "from calc import add; print(add(1, 2))"`). A test that imports the delivered code,
   or a module of its own beside it, fails to load and reads `error`. Keep each test file
   self-contained, with its expected values inside it: a reference file in the tree can be
   rewritten by the delivery before the test reads it.

2. **Start the hub** with the delivery loop switched on:

   ```bash
   ZEPHYROS_KIT=tests \
   ZEPHYROS_FORGE=github \
   ZEPHYROS_FORGE_TOKEN_FILE=/path/to/forge.token \
   ZEPHYROS_GATEWAY_KEY_FILE=/path/to/provider.key \
   ZEPHYROS_EXTRA_CONFIG="$(cat /path/to/gateway.toml)" \
     scripts/deploy.sh up
   ```

   `ZEPHYROS_KIT=tests` builds the image of the pytest kit (`images/kit-tests`) and writes the
   `[verification]` table: `runner = "sandbox"`, the kit, and its image. **Without it there is no
   runner** (`verification.runner` is `none`), and every verification ends in an error that says
   "no verification runner is configured". `ZEPHYROS_FORGE=github` writes the `[forge]` table
   (use `directory` and `ZEPHYROS_FORGE_DIR` for pull requests in a directory of the host).

   Add `ZEPHYROS_EGRESS=sealed` or `open` to change what the instances reach; the default,
   `regulated`, gives them the hub and the package registries, and not GitHub
   ([what runs](running.md#start-and-stop)). The three runs below were made before the levels
   existed, with the whole Internet open.

   `up` builds the images (the hub and filter, the network guard, the instance, and the kit),
   writes `deploy/state/zephyros.toml` with your `[gateway]` table appended, checks it with
   `zephyros config check` and stops there if it is refused, then starts the hub and the filter
   and prints the address of the operator channel (`127.0.0.1:8081`). The network guards start
   later, one per instance, when the hub starts an instance. The operator token stays in the
   hub's data volume: `scripts/deploy.sh call TOOL '{…}'` reads it from inside the hub container.

3. **Create the project**, with the calls the acceptance walks use: `project.create` (a name and
   the repository URL), `contract.import` (the Markdown of the contract), `contract.apply_reserved`
   for what only the operator may change (the requirements and constraints, the P0 criteria, the
   links to a P0 requirement), `project.prepare` (the repository and its branch: the hub takes
   the base of the staging area from the forge), and `acceptance.put` for each acceptance test
   file.

4. **The Owner side** (runs 2 and 3): `instance.start` with `"side": "owner"` and its task. The
   Owner instance proposes the stories, the criteria and their links, and puts its acceptance
   tests. Read each proposal with `contract.pending` before you approve it with
   `contract.approve`. A proposal cannot hold what is reserved to the operator (a P0 criterion,
   a link to a P0 requirement): apply those with `contract.apply_reserved`.

5. **The Contractor side**: `instance.start` with `"side": "contractor"` and the task. From here
   the real agents work: they call the hub's tools (MCP), fetch the staging base, push a branch
   and declare the delivery.

6. **The hub decides.** It replays the acceptance tests in a sandbox, computes the gate, writes
   the evidence report and opens the pull request on the forge. On GitHub, the pull request opens
   on the project's repository; the branch the hub pushes is new by construction, and the hub
   never rewrites a branch that exists.

7. **The Owner judges** the delivery ([ADR 0020](decisions/0020-the-owner-sees-and-records.md)):
   start the Owner instance again with that task. It reads the delivered branch
   (`GET /staging/delivery/<id>`, a git bundle, against `GET /staging/base`), records an
   inspection for each requirement or constraint that tests cannot show (`inspection.record`;
   until then such an object keeps the decision at CONCERNS), and records its verdict with its
   reasons (`delivery.review`), posted on the pull request. The merge stays yours.

8. **Stop**: `scripts/deploy.sh down` stops the hub and the filter and removes the guards, turns
   and sandboxes; the volumes stay. Restore the mode of the key file if you changed it.

Keep the client's interface in the contract (a constraint) when the Owner must write tests
against it: a first prompt holds 4 000 characters, and the Owner reads the code only once it is
delivered.

What to watch: `delivery.status` for the decision and the report, the log (`log.read`,
`log.verify`) for every call and every refusal, and the notifications if `[notifications]` is set
— the hub tells the main agent about decisions, loops and stalled tasks by itself (US-E7-01). The
report counts the model calls of the project that went through the gateway, with their tokens;
watch the spending at the provider as well, and start with a small project and a short task.
The hub wakes the Contractor by itself only once the contract is ready for it, so preparing the base
before the Owner side writes its tests is safe; while it waits, the log says why
([the continuity ladder](notifications.md#the-continuity-ladder)).

## What the runs show, and what they do not

- Shown with real models: the whole loop (contract, staging, delivery, sandbox replay, gate,
  report, pull request on GitHub), an Owner instance that writes and checks its own acceptance
  tests, inspections and a verdict posted on the pull request, two projects at once.
- Not shown yet: projects larger than a small Python service, other models and providers, long
  runs, and the changes of version 0.2.0 (black-box acceptance tests, the network levels, the
  Contractor's report without the run output), which the test suites cover but no real run has
  tried. The report counts only the calls that go through the gateway: an operator who drives
  a run with an agent of their own pays for it outside Zephyros. In the three runs the operator
  was an orchestrating agent (a Claude Opus 5.5 session in runs 2 and 3), not a person.
