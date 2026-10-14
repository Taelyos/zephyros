# Zephyros

> Cheap models build. An independent, model-free verification decides.

Zephyros runs a software project with AI agents the way a real engineering project is run. One
side, the **Owner**, writes the contract and the acceptance tests, then judges. The other side,
the **Contractor**, builds and delivers. Between them sits a **hub with no model in it**: it
replays the Owner's tests on every delivery in a sandbox, applies fixed rules, and opens the pull
request with an evidence report. A human merges.

Built on [Hermes Agent](https://github.com/NousResearch/hermes-agent). Version 0.2.0:
experimental, one machine, Linux or macOS with Docker.

The **operator** is the person who runs Zephyros: they write the requirements, approve what only
they may approve, and merge. The **main agent** is the agent that talks to the hub on their
behalf, between turns.

## Why

An agent that writes the code and grades it too will tell you it works. Zephyros never asks it.
The Contractor cannot write the contract, change an acceptance test or report its own verdict:
every one of those is an attack in the threat model, and the hub refuses it. The decision comes
from recorded results and written rules, the same way every time.

## What works today

- **A versioned contract.** Requirements and constraints, each with its verification method
  (test, inspection, analysis, demonstration), stories, acceptance criteria and links. Each side
  has its own rights; the P0 floor belongs to the operator. Changes are proposed, read and
  approved.
- **Acceptance tests owned by the Owner side.** The hub keeps them and puts them back in every
  verification, whatever the delivery holds; a delivery that changes one is refused. The
  Contractor knows which tests judge it (the criteria name them) and reads each one's outcome,
  but its report leaves out the output of the run, which would quote the Owner's test code. See
  [Limits](#limits) for what it can still read.
- **Deliveries through a staging area** (git bundles). The hub replays the tests in a sandbox with
  no network and canary tests of its own. Delivered code never runs in the process that records
  the results: acceptance tests are black box, and run the delivery as a subprocess
  ([ADR 0017](docs/decisions/0017-verification-runner-and-kits.md)).
- **The gate**: PASS, CONCERNS or FAIL from fixed rules (every P0, 90 % of the P1, 80 % overall).
  Constraints count as much as requirements. What tests cannot show is judged by an inspection the
  Owner records, and the gate decides again. A delivery is never PASS when no acceptance test ran
  and passed.
- **An evidence report on every pull request**: the base, head, contract and acceptance tests by
  digest, every test outcome, the rules, the cost, and flags such as a change to the client's own
  `CONTRACT.md`, and the Owner's tests that no criterion names, which never ran. The Owner's
  verdict is posted on the pull request; the title follows the decision.
- **Isolation**: one network and one guard per instance, a token per project and side, protected
  networks unreachable, a model gateway that holds the provider key and sends only the models the
  configuration authorises, and a hash-chained log of every call and refusal, whose head is also
  kept in a separate anchor file, so that a replaced or truncated database is detected (see
  [Limits](#limits) for what the anchor does not stop).
- **Network access in three levels** ([ADR 0024](docs/decisions/0024-three-levels-of-network-access.md)).
  By default (`regulated`) an instance reaches the hub and the package registries, through a proxy
  in the hub that logs every destination; `sealed` reaches the hub alone; `open` reaches the
  Internet, as version 0.1.0 did.
- **The main agent is told** of every decision (refusals and errors included), loops of failed
  deliveries, verdicts, and stalled tasks, through signed webhooks. Each is sent once, in order,
  from a queue of its own, and its outcome is logged; one that fails is not sent again (see
  [Limits](#limits)). The continuity ladder that wakes, restarts and escalates a stalled instance
  survives a restart of the hub.
- **A threat model of 93 attacks**, kept with the project's working documents (not published).
  66 have at least one automated test that carries their identifier: 60 in the acceptance and
  runtime suites, run as the attacker would, and 6 in unit tests only. 25 of the others belong to
  features not built yet (signed approvals, budget, dashboard, backups, previews, automated
  delivery checks). For the last 2, no test carries their identifier: a repository URL that points
  at a private host (ATK-75), and an instance that re-submits a rejected delivery in a loop or
  reports stale work (ATK-79). Part of their scenarios is covered by tests filed under ATK-74 and
  EXG-44 (repository URLs such as `file:///` or a private address are refused) and EXG-23 (the
  alert for a loop of failed deliveries). A test that names an attack shows that the hub refuses
  that attack as written, not that every variant of it is closed.

## Three runs with real models

DeepSeek V4.1 Flash on the Owner and Contractor sides, through OpenCode Go, on small Python
projects, on one Linux machine. The operator's part (writing the requirements, approving the
changes, starting the instances) was not played by a person but by an orchestrating agent, a
Claude Opus 5.5 session in runs 2 and 3. Its model calls did not go through the gateway and are
**not** in the table.

| Run | Project | Real agents | Result | Model calls | Tokens in / out | Cost* |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | steam-watch (new) | Contractor; the contract and tests were written by the operator | the 17th delivery passed 4/4, after the run exposed bugs in Zephyros itself, fixed as it went | 106 | 5.9 M / 75 k | $0.94 |
| 2 | homelab-home (new) and steam-watch v2, in parallel | Owner and Contractor | 3 deliveries, PASS 9/9, 7/7 and 10/10; the Owner checked its own tests against 18 wrong implementations (all caught) | 278 | 14.0 M / 336 k | $2.31 |
| 3 | homelab-home v1.1 | Owner and Contractor | CONCERNS (two constraints awaited inspection), then PASS once the Owner inspected the code; its verdict posted on the pull request | 173 | 23.3 M / 191 k | $3.6 |

\* The calls of the Owner and Contractor sides through the gateway, as the hub counted them.
Indicative, at the provider's list price ($0.15 per million tokens in, $0.60 out), before cache
discounts; the runs were covered by a flat subscription.

Each run found something the simulated suite could not. Run 2: the approver could not read a
proposal, the gate ignored constraints, the Owner judged without seeing the code, and a Contractor
rewrote the client's `CONTRACT.md` unnoticed ([ADR 0020](docs/decisions/0020-the-owner-sees-and-records.md)).
Run 3: a provider error said nothing, conversations grew to 188,000 tokens a call, and Hermes
reviewed each turn after it ended. Each has a fix in the code; the cap on the size of a
conversation (compaction past 80,000 tokens) has not been tried in a real run yet.

Memory: two projects with one real turn each used 924 MiB in all, measured once. One turn alone
reached 1,019 MiB, and a turn may now use up to 1,536 MiB, so two active projects are not held to
2 GB in every case.

The runs used the code of version 0.1.0 and before it. The changes of 0.2.0 are proven by the test
suites, not yet by a run with real models.

## Demo

A recorded run can be replayed: `tools/replay/` holds an exporter that turns the hub's database
into one JSON document, a self-contained player page, and a recorder that makes a video of it
([replay format](docs/replay-format.md)). `tools/replay/example.replay.json` is a synthetic run
written by test code. A replay of a real run and a video are not published yet.

## Quick start

Needs Docker (OrbStack works on macOS), [uv](https://docs.astral.sh/uv/) 0.12.24 exactly (the
project pins it) and Python 3.12 or later.

```bash
git clone https://github.com/Taelyos/zephyros && cd zephyros
uv sync --locked
scripts/check.sh        # lint, types, unit and acceptance suites; no Docker, but network access

scripts/deploy.sh up    # hub and filter, operator channel on 127.0.0.1:8081, simulated model
scripts/deploy.sh call project.create '{"name": "demo"}'
scripts/deploy.sh call project.list
scripts/deploy.sh down
```

This starts the hub with no verification kit: a delivery would end in an error. The delivery
loop needs `ZEPHYROS_KIT=tests`; [a run with real models](docs/real-test.md) shows the whole
command, with the gateway, the forge and the kit.

### The tests

`scripts/check.sh` runs the unit and acceptance suites; `scripts/acceptance.sh` runs the runtime
suite, on real containers. As collected by pytest, parametrized cases each counted:

| Suite | Tests | What it covers |
| --- | --- | --- |
| `tests/unit/` | 2,366 | The hub, its adapters, the configuration, the kit, the replay tools; no container |
| `tests/acceptance/` | 533 | Whole stories and attacks of the threat model through a real hub, with fakes for containers and the forge |
| `tests/runtime/` | 141 | Real containers: the turn's hardening, the network guard and levels, the sandbox and the kit, real Hermes turns with a scripted model |

Without a container runtime, `scripts/check.sh` reports 2,898 passed and 142 skipped: every
skipped test needs a container runtime (the runtime suite, with the conformance cases it runs
against the real sandbox). It fails below 85 % branch coverage of the hub; on the CI run of this
release, 1,716 of the hub's 1,842 branches are covered (93 %), and 96.7 % of the code in all,
which counts the conformance suites and the dummy implementations shipped in the package.

## Limits

What holds and what does not, in version 0.2.0:

- **One machine, IPv4 only.** Linux and macOS; not Windows. The CI runs the unit and acceptance
  suites on both, but the runtime suite (real containers) on Linux only, and the three real runs
  were on Linux.
- **One verification kit**, `tests` (pytest, Python 3.12). Its acceptance tests are black box:
  they run the delivery as a subprocess and read its output. A test that imports the delivered
  code, or a support module next to it, fails to load and reads `error`. Instances run Python 3.14:
  a test that depends on the minor version of Python may behave differently in the instance and
  in the sandbox.
- **The Owner's other files in the sandbox can be rewritten by the delivery.** The kit holds the
  acceptance test files to what the hub sent, but a reference file or a helper script that a test
  reads from the working directory can be replaced by the delivered code before the test reads it.
  Keep the expected values inside the test file ([ADR 0017](docs/decisions/0017-verification-runner-and-kits.md)).
- **A delivery can make its own verification unreadable** (a flooded log, lines that look like
  the kit's). That turns the verdict into an error, never into a pass.
- **What the Contractor can still read of the Owner's tests**: their names and outcomes; the names
  and digests of the Owner's files in its report; any test file the client keeps in the
  repository the base is taken from; and the full report, run output included, on the pull
  request, if it can reach the forge (at the `open` level, or when the operator lists the forge's
  host in `network.allowed_hosts`).
- **An allowed registry is still a channel** at the `regulated` level: data can be encoded in the
  names and paths an instance asks for, and the log keeps the host, not the path. The proxy checks
  the name of a connection, not what goes through it, and does not make a registry read-only
  ([ADR 0024](docs/decisions/0024-three-levels-of-network-access.md)). Installing a package runs
  code from the registry inside the instance. `open` reaches the whole Internet.
- **What the hub does not see of the network.** A client that ignores `HTTPS_PROXY` reaches
  nothing, but the guard drops its attempts without a record in the hub's log. There is no ceiling
  on the tunnels an instance opens at once, and refusals of the proxy do not count toward
  `limits.refusals_per_minute`: an authenticated instance can fill the log with `instance.egress`
  records.
- **Notifications are sent once.** One that fails (the receiver is down, answers an error, or
  takes too long) is logged and not sent again; the queue holds 1,000 notifications, and what is
  still queued when the hub stops is logged as `hub_stopped`, not sent. The log and
  `delivery.status` keep the facts; the main agent must read them after an outage
  ([webhooks](docs/webhooks.md)).
- **The continuity ladder waits for a contract that names an acceptance test.** A contract judged
  by inspection alone starts no ladder for the Contractor. One named test is enough, so a contract
  written in proposals approved further apart than `continuity.inactivity_seconds` can wake the
  Contractor in between. An Owner side that proposes and rejects its own change again and again
  ends its decision task each time
  ([ADR 0021](docs/decisions/0021-the-ladder-waits-for-a-ready-contract.md),
  [ADR 0023](docs/decisions/0023-a-decision-is-awaited-from-whoever-takes-it.md)).
- **The log anchor is a file next to the database.** It detects a database replaced or cut on its
  own, but whoever can write the hub's data directory can forge both the log and the anchor; the
  anchor belongs on another medium when that matters. A lost anchor keeps the log verification
  failing: back it up with the database; there is no re-anchoring yet
  ([ADR 0019](docs/decisions/0019-anchor-outside-the-store.md)).
- **The model policy is the configuration file.** The gateway sends only the models `gateway.models`
  names; the policy is as safe as the file that holds it.
- The gate judges what the contract says, nothing more: a contract with no design requirement
  gets a page with no design.
- **Not built yet**: approvals signed by the human (the operator's token approves), budgets,
  a dashboard, backups, previews, and the automated delivery checks for vulnerable dependencies
  and committed secrets (EXG-35: the report says "Not run by this version of the hub"). The 25
  attacks of the threat model that target these features have no test.
- Three real runs, all small Python projects, driven by an agent in the operator's place. Not a
  benchmark. Turns take minutes and real runs cost tokens.

## How it is built

| Part | Role |
| --- | --- |
| hub | Holds the contracts, tokens and log; serves the operator channel, the main agent's endpoint and, on each instance network, the agent endpoint (MCP tools and the model gateway) and the egress proxy. No model inside. |
| filter | The only process that reaches the container runtime; no network, a closed list of operations. |
| guard | One per instance: its network rules, at the level the configuration sets, and its fixed address. |
| turn | The pinned Hermes Agent, unprivileged, for one piece of work. |

Zephyros is not built inside Zephyros. A written specification came first, and it evolves
through product decisions; the work goes through small pull requests, each checked by the CI and
by an independent review. [The architecture decisions](docs/decisions/) explain the choices.

## Documentation

- [Running Zephyros on one machine](docs/running.md) and [a run with real models](docs/real-test.md)
- [Architecture decisions](docs/decisions/)
- [Public interfaces](docs/interfaces.md), [tools of the hub](docs/tools.md),
  [tools of an instance](docs/instance-tools.md), [configuration](docs/configuration.md)
- [Notifications](docs/notifications.md) and [webhooks](docs/webhooks.md)
- [Error messages](docs/errors.md)
- [Changelog](CHANGELOG.md) and [release notes](docs/release-notes/)
- [Development guide](docs/development.md)

## Related

AgoraCraft, a separate project, is an optional multiplayer Minecraft HQ that will show Zephyros
projects as construction sites.

## Acknowledgements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT), the runtime
  Zephyros is built on.
- The [BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD) (MIT), an inspiration for
  document templates and quality gates.
- [AgentCraft](https://github.com/blendi-remade/agentcraft) (MIT), for its ideas, such as lead
  review followed by a human merge.

## License

[MIT](LICENSE)
