# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

Zephyros runs software projects with AI agents the way a real engineering project is run: inside a project it runs, an owner agent writes the contract and verifies, a contractor agent builds and proves. Read [README.md](README.md) first.

In this repository, **"contract" means that product feature only**: what binds the owner and contractor agents of a project that Zephyros runs.

## How Zephyros itself is developed

The development of Zephyros does not run inside Zephyros. It is run by a small product team:

| Role | Does |
| --- | --- |
| Project owner (a person) | Decides what Zephyros does: priorities, product decisions, acceptance of a release |
| Coordinating agent | Splits the work into tickets, assigns them, has each pull request reviewed, merges once the CI is green |
| Worker sessions (assume this is you) | Build one ticket on a branch: design, code, tests, docs, then a pull request |
| Independent reviewers | Fresh sessions that check a pull request against the code and the ticket, and report what does not hold |

The working specification (requirements EXG/CTR, user stories, threat model and its attack catalog) lives outside this public repository. It is **living, not contractual**: when the code and the specification disagree, or the specification looks wrong or ill-suited, say so in your report; a product decision, recorded as an ADR, settles it, and the specification follows.

## Read before working

1. `CLAUDE.local.md`, if present: where the working documents live (specification, design notes). They are kept outside this public repository.
2. [docs/decisions/](docs/decisions/): the architecture decisions. Do not contradict them silently.
3. [docs/interfaces.md](docs/interfaces.md) and [docs/development.md](docs/development.md).

## Working rules

- **Stay on your ticket**, on its branch. Never push to `main`, never merge: the coordinating agent merges once the CI is green.
- **Write the code and the tests**, including the executable attacks of the threat model in `tests/acceptance/` and `tests/runtime/`. Reviewers read them, run them and point out gaps.
- **Never declare a requirement verified.** Report what you did, how to run the checks, and what you are unsure about.
- **Raise significant design choices** in your report or in the working documents (see `CLAUDE.local.md`), and record important decisions as an ADR in `docs/decisions/` with status `Proposed`; the project owner accepts or reverses them.
- **Keep this repository a showcase.** Only code, tests and user-facing documentation go here. Design notes, spike reports, questions and reviews go to the working documents, and nothing here refers to them.
- **Work in small increments**, one ticket or one coherent piece at a time. Reference story, requirement or ADR IDs in commit messages when they apply.
- **Aim above the minimum in quality, not in scope.** Care, robustness, good tests, readable code and docs are expected. Features nobody asked for stay out.

## Constraints

- Platform and process (CTR-01 to CTR-07): instances use a pinned Hermes Agent (CTR-01) in containers (CTR-03); the hub is written in Python (CTR-02); GitHub is the forge (CTR-04); macOS and Linux (CTR-05); MIT license (CTR-06); all repository content is in English (CTR-07).
- Quality and supply chain (CTR-08 to CTR-14): strict type checking (CTR-08); CI checks on every change to the main branch (CTR-09); dependencies, images and CI actions pinned by hash (CTR-10); versioned public interfaces with dummy implementations (CTR-11); signed, attested published versions (CTR-12); semantic versioning of the public API (CTR-13); an evidence report with every published version (CTR-14).
- Hermes is pinned. Work against a separate checkout with its own `HERMES_HOME`. **Never touch an existing Hermes installation in `~/.hermes`**, never run `hermes update` against it, never create profile aliases in `~/.local/bin`.
- Any Docker-compatible runtime works (OrbStack works well on macOS).
- This repository is public: no personal data, machine names, credentials or private network details.

## Working economically

Model usage costs money. Keep sessions short and focused on one story, and `/clear` between stories. Use a stronger advisor for planning and hard debugging, and small sub-agents only for tightly scoped tasks with a precise input and output.
