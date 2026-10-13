# Zephyros

> Cheap models build. An independent, model-free verification decides.

Zephyros runs a software project with AI agents the way a real engineering project is run. One
side, the **Owner**, writes the contract and the acceptance tests, then judges. The other side,
the **Contractor**, builds and delivers. Between them sits a **hub with no model in it**: it
replays the Owner's tests on every delivery in a sandbox, applies fixed rules, and opens the pull
request with an evidence report. A human merges.

Built on [Hermes Agent](https://github.com/NousResearch/hermes-agent). Version 0.1.0:
experimental, one machine, Linux or macOS with Docker.

## Why

An agent that writes the code and grades it too will tell you it works. Zephyros never asks it.
The Contractor cannot write the contract, read the Owner's private state, change an acceptance
test or report its own verdict: every one of those is an attack in the catalog, and the hub
refuses it. The decision comes from recorded results and written rules, the same way every time.

## What works today

- **A versioned contract.** Requirements and constraints, each with its verification method
  (test, inspection, analysis, demonstration), stories, acceptance criteria and links. Each side
  has its own rights; the P0 floor belongs to the operator. Changes are proposed, read and
  approved.
- **Acceptance tests owned by the client side.** The Contractor never sees them; a delivery that
  touches one is refused.
- **Deliveries through a staging area** (git bundles). The hub replays the tests in a sandbox with
  no network and its own canary tests.
- **The gate**: PASS, CONCERNS or FAIL from fixed rules (every P0, 90 % of the P1, 80 % overall).
  Constraints count as much as requirements. What tests cannot show is judged by an inspection the
  Owner records, and the gate decides again.
- **An evidence report on every pull request**: the base, head, contract and acceptance tests by
  digest, every test outcome, the rules, the cost, and flags such as a change to the client's own
  `CONTRACT.md`. The Owner's verdict is posted on the pull request; the title follows the decision.
- **Isolation**: one network and one guard per instance, a token per project and side, protected
  networks unreachable, a model gateway that holds the provider key and serves one authorised model,
  and a hash-chained log of every call and refusal.
- **A threat model of 93 attacks.** 59 have automated tests, most of them run as the attacker
  would; 25 wait for features of the next milestone (signed approvals, budget, dashboard,
  backups); 9 have no automated test yet. 2,400+ tests in all, 96 % coverage.

## Three runs with real models

DeepSeek V4.1 Flash on every side, through OpenCode Go, on small Python projects.

| Run | Project | Real agents | Result | Model calls | Tokens in / out | Cost* |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | steam-watch (new) | Contractor; the contract and tests were written by the operator | the 17th delivery passed 4/4, after the run exposed bugs in Zephyros itself, fixed as it went | 106 | 5.9 M / 75 k | $0.94 |
| 2 | homelab-home (new) and steam-watch v2, in parallel | Owner and Contractor | 3 deliveries, PASS 9/9, 7/7 and 10/10; the Owner checked its own tests against 18 wrong implementations (all caught) | 278 | 14.0 M / 336 k | $2.31 |
| 3 | homelab-home v1.1 | Owner and Contractor | CONCERNS (two constraints awaited inspection), then PASS once the Owner inspected the code; its verdict posted on the pull request | 173 | 23.3 M / 191 k | $3.6 |

\* Indicative, at the provider's list price ($0.15 per million tokens in, $0.60 out), before
cache discounts; the runs were covered by a flat subscription.

Each run found something the simulated suite could not. Run 2: the approver could not read a
proposal, the gate ignored constraints, the Owner judged without seeing the code, and a Contractor
rewrote the client's `CONTRACT.md` unnoticed ([ADR 0020](docs/decisions/0020-the-owner-sees-and-records.md)).
Run 3: a provider error said nothing, conversations grew to 188 000 tokens a call, and Hermes
reviewed each turn after it ended. All are fixed. Two active projects fit in 2 GB of memory
(measured: 924 MiB with a real turn in each).

## Demo

A replay of a recorded run and a two-minute video are in the works.

## Quick start

Needs Docker (OrbStack works on macOS), [uv](https://docs.astral.sh/uv/) 0.12.24 and Python 3.12.

```bash
git clone https://github.com/Taelyos/zephyros && cd zephyros
uv sync --locked
scripts/check.sh        # lint, types, unit and acceptance suites; no Docker needed

scripts/deploy.sh up    # hub and filter, operator channel on 127.0.0.1:8081, simulated model
scripts/deploy.sh call project.create '{"name": "demo"}'
scripts/deploy.sh call project.list
scripts/deploy.sh down
```

With a real model: [docs/real-test.md](docs/real-test.md).

## Limits

- One machine, IPv4 only. Linux and macOS; not Windows.
- One authorised model at a time; the hub refuses to start with any other.
- The gate judges what the contract says, nothing more: a contract with no design requirement
  gets a page with no design.
- Three real runs, all small Python projects. Not a benchmark yet.
- Turns take minutes and real runs cost tokens. No dashboard yet.

## How it is built

| Part | Role |
| --- | --- |
| hub | Holds the contracts, tokens and log; serves the operator channel, the main agent's endpoint and, on each instance network, the agent endpoint (MCP tools and the model gateway). No model inside. |
| filter | The only process that reaches the container runtime; no network, a closed list of operations. |
| guard | One per instance: its network rules and fixed address. |
| turn | The pinned Hermes Agent, unprivileged, for one piece of work. |

Zephyros is built with its own method: a written contract came first, and each increment is
recorded against it. [20 architecture decisions](docs/decisions/) explain the choices.

## Documentation

- [Running Zephyros on one machine](docs/running.md) and [a run with real models](docs/real-test.md)
- [Architecture decisions](docs/decisions/)
- [Public interfaces](docs/interfaces.md), [tools of the hub](docs/tools.md),
  [tools of an instance](docs/instance-tools.md), [configuration](docs/configuration.md)
- [Notifications](docs/notifications.md) and [webhooks](docs/webhooks.md)
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
