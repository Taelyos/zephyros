# Running the real test (with real models)

Everything up to this point runs with the **simulated** model provider: the obedient stub
answers the instances, the whole delivery loop works, and nothing spends. The real test is the
same loop with real agents behind the gateway. It is the one step that is not exercised yet —
by decision, it waits for a human.

## What the real test is

The hub never talks to a model provider itself (CTR-11): it is a gateway. The instances run
Hermes, which asks the gateway; the gateway forwards to an OpenAI-compatible provider with the
credential file the configuration names. Swapping the provider is the whole change:

```toml
[gateway]
provider = "openai-compatible"
upstream_url = "https://provider.example/v1"
model = "served-model"
upstream_model = "deepseek-v4.1-flash"
models = ["deepseek-v4.1-flash"]
credential_file = "/run/zephyros-credentials/gateway.key"
session_header = "x-opencode-session"
simulation = false
```

`session_header` names the session header the provider asks for — OpenCode Go refuses a call
without `x-opencode-session` (HTTP 400), and the gateway sends one stable session name per
process. A provider that asks for none: leave it unset.

Rules the schema enforces: `upstream_url` and `credential_file` are set together; the model it
serves is one of `gateway.models`, which the start checks against the code's policy
(`AUTHORISED_MODELS`); `simulation = true` runs only with the stub (a real provider is not a
simulation); the credential file is read at every call, so rotating it takes effect on the
next call.

**Where the key file lives.** The hub runs in a container: the key is not copied into it. The
file stays where it is on the host and `scripts/deploy.sh` bind-mounts it, read-only, at
`/run/zephyros-credentials/gateway.key` — the path the configuration names. The hub runs as
uid 10001, so the file must be readable by that uid: give it exactly that one read ACL,
`setfacl -m u:10001:r <the file>`, rather than opening it to everyone. The same holds for the
token of a GitHub forge, mounted at `/run/zephyros-credentials/forge.key`. With no key given,
deploy.sh mounts an empty file instead, so the variables always resolve.

## How to run it

1. On the machine that hosts the stack (a single Linux host in the development setup), keep the credential
   in its file (for the first run: the OpenCode key, kept in a credentials directory outside the repository), give the hub
   uid its read ACL, and let the `[gateway]` table name the container path
   `/run/zephyros-credentials/gateway.key`; pass the host path in `ZEPHYROS_GATEWAY_KEY_FILE`.
   For a GitHub forge, pass the token file in `ZEPHYROS_FORGE_TOKEN_FILE` and set
   `ZEPHYROS_FORGE=github`.
2. `scripts/deploy.sh up` — it builds, writes the configuration (with your `[gateway]` table
   merged), mounts the credential files, starts the hub, the filter and the guard, and prints
   the operator token.
3. Create the project and prepare the staging area — the same calls the acceptance walks use:
   `project.create`, `contract.apply_reserved` (or `contract.import` with the project's
   Markdown), `project.prepare`, `acceptance.put`.
4. Start the Contractor instance with the task:
   `instance.start(project_id, side="contractor", prompt="<the task>")`.
   From here the real agents work: they call the hub's tools (MCP), fetch the staging base,
   push a branch, declare the delivery.
5. The hub replays the acceptance tests in a sandbox, computes the gate, writes the evidence
   report and opens the pull request on the forge. On GitHub, the pull request opens on the
   project's repository; the branch the hub pushes is new by construction, and the hub never
   rewrites a branch that exists.
6. The Owner instance judges the delivery (ADR 0020): it reads the delivered branch
   (`GET /staging/delivery/<id>`, a git bundle, against `GET /staging/base`), records an
   inspection for each requirement or constraint that tests cannot show (`inspection.record`;
   until then such an object keeps the decision at CONCERNS), and records its verdict with its
   reasons (`delivery.review`), posted on the pull request. Before approving a change of the
   contract that the Owner proposed, read it with `contract.pending`. The merge stays yours.

Keep the client's interface in the contract (a constraint) when the Owner must write tests
against it: a first prompt holds 4 000 characters, and the Owner reads the code only once it
is delivered.

What to watch: `delivery.status` for the decision, the log (`log.read`, `log.verify`) for
every call and every refusal, and the notifications if `[notifications]` is set — the hub
tells the main agent about decisions, loops and stalled tasks by itself (US-E7-01).

## What is proven, and what is not

- Proven with the simulated provider, on the real organs: the whole loop (contract, staging,
  delivery, sandbox replay, gate, report, pull request), the continuity ladder, the egress
  policy, the masking, the log's integrity — see the acceptance suite and
  `design/Z3-verification.md`.
- Not proven: how real models behave inside the same loop — turn lengths, retries, cost, the
  quality of their contract proposals. The first real run should start with a small project
  and a short task, and its cost should be watched at the provider.
