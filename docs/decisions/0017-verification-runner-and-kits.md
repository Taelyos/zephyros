# 0017. A verification runner runs one verification kit; the method does not change with the project

- Status: Proposed
- Date: 2026-10-10

## Context
The method of Zephyros is the same for every project: requirements with their verification methods, stories and criteria linked to them, results recorded by the hub, a gate of fixed rules ([0005](0005-deterministic-acceptance-gate.md)). What changes from one project to another is how the delivered code is started and observed. A library is verified by its tests. A web application needs its tests and a browser that reads the page. A simulation is compared with a reference scenario, within tolerances. Infrastructure code is verified by its plan and by policy checks. A Minecraft plugin needs a server and a bot that plays. All of these run delivered code, so all of them run in the sandbox ([0004](0004-sandboxed-execution-of-delivered-code.md)).

## Decision
- A **verification runner** is a public interface of the hub (CTR-11), versioned, with a dummy implementation and a conformance suite. Given a tree to verify, the acceptance tests to run and a kit, it starts the project, observes it, and returns one outcome per acceptance test (passed, failed, error, not run), with bounded evidence (a log excerpt, and files such as screenshots). It renders no decision.
- A **verification kit** is the part that changes: how to start the project and how to observe it. A kit is an image and a contract between the runner and that image (where the tree is, how the tests are named, where the outcomes and the evidence are written). The configuration of a project names its kit.
- The first kit is **tests**: the project's test runner (pytest first). The second is **tests and a headless browser**: the same, plus Playwright reading the page and taking screenshots as evidence. Later kits (simulation, infrastructure, Minecraft with MockBukkit, a headless Paper server and a Mineflayer bot) add an image and a contract, and change nothing in the gate.
- Subjective checks stay human, at the demonstration: no kit judges taste or intent.
- The runner of the hub runs every kit in a sandbox created through the container filter: no network, no secret, limits, destroyed at the end.

## Consequences
- Positive: the gate, the evidence report and the traceability are the same for every kind of project.
- Positive: a new kind of project is a new kit, written and tested on its own against the conformance suite.
- Negative: each kit image is something more to pin, build and keep up to date.
- Negative: a kit decides how outcomes are written. Until the runner keeps them out of the delivered code's reach (EXG-37), a kit's outcomes can be forged by the code it runs.

## Alternatives considered
- One runner per kind of project, each with its own notion of a result: the gate would have to know every kind.
- Letting the Contractor provide the verification scripts: the Contractor never verifies (EXG-08).
