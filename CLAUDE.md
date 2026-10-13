# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

Zephyros runs software projects with AI agents the way a real engineering project is run: an owner side writes the contract and verifies, a contractor side builds and proves. Read [README.md](README.md) first.

Zephyros is built **with its own method**. Until the hub exists, the roles are played by separate Claude Code sessions:

| Role | Who | Does |
| --- | --- | --- |
| Owner | The human and an owner session | Writes and changes the contract, validates designs, verifies deliveries, decides acceptance |
| Contractor | Any other session in this repository (assume this is you) | Proposes the design, implements user stories, writes its own tests, delivers |

## Read before working

1. `CLAUDE.local.md`, if present: where the project's working documents live (contract, user stories, threat model, design notes). They are kept outside this public repository.
2. [docs/decisions/](docs/decisions/): accepted architecture decisions. Do not contradict them silently.
3. [docs/interfaces.md](docs/interfaces.md) and [docs/development.md](docs/development.md).

## Contractor rules

While Zephyros is being built, the workflow is simple: you build, the owner verifies.

- **Do not edit the contract.** It is the reference. If something in it is wrong, unclear or missing, say so in your report; the owner fixes it.
- **Write the code and the tests**, including the executable attack catalog in `tests/acceptance/`. The owner reviews them, runs them and adds attacks where it sees gaps.
- **Never declare a requirement verified.** Report what you did, how to run the checks, and what you are unsure about.
- **Discuss significant design choices first** in the working documents (see `CLAUDE.local.md`), and record important decisions as an ADR in `docs/decisions/` with status `Proposed`.
- **Keep this repository a showcase.** Only code, tests and user-facing documentation go here. Design notes, spike reports, questions and reviews go to the working documents.
- **Work in small increments**, one story or one coherent piece at a time. Reference story or requirement IDs in commit messages when they apply.
- **Aim above the minimum in quality, not in scope.** Care, robustness, good tests, readable code and docs are expected. Features nobody asked for stay out.

## Constraints

- Platform and process (CTR-01 to CTR-07): instances use a pinned Hermes Agent (CTR-01) in containers (CTR-03); the hub is written in Python (CTR-02); GitHub is the forge (CTR-04); macOS and Linux (CTR-05); MIT license (CTR-06); all repository content is in English (CTR-07).
- Quality and supply chain (CTR-08 to CTR-14): strict type checking (CTR-08); CI checks on every change to the main branch (CTR-09); dependencies, images and CI actions pinned by hash (CTR-10); versioned public interfaces with dummy implementations (CTR-11); signed, attested published versions (CTR-12); semantic versioning of the public API (CTR-13); an evidence report with every published version (CTR-14).
- Hermes is pinned. Work against a separate checkout with its own `HERMES_HOME`. **Never touch an existing Hermes installation in `~/.hermes`**, never run `hermes update` against it, never create profile aliases in `~/.local/bin`.
- Any Docker-compatible runtime works (OrbStack works well on macOS).
- This repository is public: no personal data, machine names, credentials or private network details.

## Working economically

Model usage costs money. Keep sessions short and focused on one story, and `/clear` between stories. Use a stronger advisor for planning and hard debugging, and small sub-agents only for tightly scoped tasks with a precise input and output.
