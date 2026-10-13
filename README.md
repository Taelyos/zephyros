# Zephyros

> Project engineering for autonomous AI agents, built on [Hermes Agent](https://github.com/NousResearch/hermes-agent).

**Status: early design. There is nothing to install yet.**

## What it is

Zephyros runs software projects with AI agents the way a real engineering project is run. An owner side owns the requirements and verifies every deliverable independently. A contractor side builds, and has to prove that what it built works.

- **A written contract.** Numbered requirements, each stating up front how it will be verified: test, inspection, analysis or demonstration.
- **Acceptance tests written before the code**, by the owner side. The contractor can read them but never change them.
- **Independent verification.** The contractor tests its own work, then everything it delivers is checked again by the owner side, in a separate context, against the contract.
- **Traceability** from each requirement to its user stories, tests and demos, up to a formal acceptance.
- **A process level and a strictness level**, set per request, so that changing a button color takes minutes, not months.
- **Full observability**: tokens and cost per agent, side, model and requirement.

## Roles

| Side | Role | What it does |
| --- | --- | --- |
| Owner | Human and main agent | Express the need, arbitrate, accept the delivery |
| Owner | Owner's assistants (optional) | Help the main agent, fully on the human's side |
| Owner | Expert agents | Preparation (needs, UX, requirements) or verification (testing, security, code review) |
| Contractor | Architect | Proposes the architecture, splits the work, reviews internally |
| Contractor | Workers | Write and test the code, each in its own git worktree |

## How it is built

Zephyros is a distribution of Hermes Agent. Hermes, pinned to a known version, is the back end: profiles, delegation, kanban, cron, approval rules and model providers. Zephyros adds:

- the method: agent profiles, skills and document templates;
- a hub: a service without AI that holds the contract, enforces each side's rights, runs verifications and computes the acceptance decision;
- a dashboard for the project mode;
- a one-command installer.

## Documentation

- [Architecture decisions](docs/decisions/)
- [Public interfaces](docs/interfaces.md)
- [Tools of the hub](docs/tools.md) and [tools of an instance](docs/instance-tools.md)
- [Running Zephyros on one machine](docs/running.md)
- [Development guide](docs/development.md)

Zephyros is built with its own method: a written contract came first, and every delivery is verified against it. Architecture and security overviews will be published with the first release.

## Related

AgoraCraft, a separate project, is an optional multiplayer Minecraft HQ that will show Zephyros projects as construction sites.

## Acknowledgements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT), the runtime Zephyros is built on.
- The [BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD) (MIT), an inspiration for document templates and quality gates.
- [AgentCraft](https://github.com/blendi-remade/agentcraft) (MIT), for its ideas, such as lead review followed by a human merge.

## License

[MIT](LICENSE)
