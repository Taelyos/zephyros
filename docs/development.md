# Development

## Set up

You need Python 3.12 or later and [`uv`](https://docs.astral.sh/uv/) 0.12.24 or later. Any way of installing `uv` is fine, for example:

```bash
python3 -m venv ~/.local/share/uv-bootstrap && ~/.local/share/uv-bootstrap/bin/pip install uv
```

Then put `uv` on your `PATH`. Every other tool (type checker, linter, test runner, audit, secret scan) comes from the lock file and is installed by `uv sync --locked`.

## Run every check

```bash
scripts/check.sh
```

This is the exact sequence the CI runs (`.github/workflows/ci.yml`): lock up to date, pins by hash, type-checker suppressions, core purity, import contracts, lint, format, strict types, tests with 85 % branch coverage, vulnerable dependencies, secrets. It needs network access for the vulnerability audit.

The tests of the replay player (`tests/unit/tools/test_replay_player.py`) run it in headless Chromium or Chrome, found on the `PATH` or named by `CHROMIUM`; on a machine with neither they are skipped as not applicable.

## Run the runtime suite

```bash
scripts/acceptance.sh
```

The tests that need a container runtime (`tests/runtime/`): the hardening of a turn and the network containment, on real containers built from the filter's templates, behind the real network guard (`images/guard/`). The agent engine is replaced by a probe image that waits, so that each attempt of the attack catalog runs inside a turn as its user. Every blocked attempt has a control that takes the guard's rules off and shows the same attempt arriving. The tests of `tests/runtime/test_hermes.py` start a whole Zephyros with `scripts/deploy.sh` (its own names, ports and address pool) and run real Hermes turns with the obedient stub as the model; they read what each tool returned from the session database of Hermes. It takes several minutes (the first run pulls the Hermes image, about 1 GB), builds its images, and removes everything it creates. The CI runs it on Linux (job `runtime`). A step that cannot apply on the machine (IPv6 on a network without it, the Internet when offline) is reported as skipped with the reason "not applicable", never as passed.

## What the repository settings must enforce

A failing workflow does not stop a merge by itself. Once the repository has a remote, the human sets branch protection on `main` so that both jobs `checks (ubuntu-latest)` and `checks (macos-latest)` are required. Without it, "the CI shall reject the change" (US-E11-04, US-E11-05, US-E11-03) is a convention, not a rule.

## Known limits of the pins (CTR-10)

- The build backend is `uv_build`, the backend bundled in `uv`. `uv` runs it without downloading it, so `[build-system]` carries an exact version but no hash; it is tied to the pinned `uv` (`required-version`, and `version` in the workflow).
- The CI installs `uv` with `astral-sh/setup-uv`, pinned by commit hash. Whether the action verifies the checksum of the `uv` binary it downloads is to be confirmed on the first run of the workflow.
- `deploy/compose.yaml` takes the image of the hub and the filter from the variable `${ZEPHYROS_IMAGE:?}`, which has no default. `scripts/deploy.sh` sets it to the identifier of the image it built (`sha256:…`) and refuses anything else. The pin checker accepts a required variable with no default in a compose file, and nothing looser.
- The bootstrap command above (`pip install uv`) is not hashed. It installs the tool that then enforces the hashes.

## Layout

| Path | Content |
| --- | --- |
| `src/zephyros/core/` | The hub core: pure code, standard library only |
| `src/zephyros/adapters/` | Container runtime, storage, HTTP, agent engine, model provider |
| `tools/` | The repository checks (pins, suppressions, core purity) and the replay tools (`tools/replay/`, [replay format](replay-format.md)). Not part of the hub |
| `tests/unit/` | Unit tests, no container runtime |
| `tests/runtime/` | Tests that need a container runtime (`scripts/acceptance.sh`) |
| `tests/acceptance/` | The executable attack catalog |
| `schemas/` | Versioned JSON Schemas, generated from the code |
| `images/` | The images Zephyros builds: the network guard |
| `scripts/` | `check.sh`, `acceptance.sh` |

## Conventions

- A suppression of the type checker names an error code and gives a reason in the same comment: `x = f()  # type: ignore[no-any-return]  # reason: the stub lacks a type`. The CI rejects anything else.
- A dependency, a container image or a CI action is referenced by hash. The CI rejects anything else.
- Commit messages start with the increment and the story or requirement IDs: `Z2-00 (US-E11-04, CTR-09): ...`.
