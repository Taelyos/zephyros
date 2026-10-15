# 0031. The agent has every power inside its instance

- Status: Accepted
- Date: 2026-10-15
- Decided by the project owner on 2026-10-15 for the direction: inside the instance, Hermes neither refuses nor asks; what protects is around the agent. The design below is the coordinating agent's.
- Extends: [0002](0002-guarantees-from-os-and-hub.md), [0016](0016-one-agent-container-per-turn.md), [0029](0029-the-contractor-can-be-a-third-party-coding-agent.md)

## Context

A turn of an instance is one Hermes process, `hermes chat --oneshot`, with nobody to answer it ([ADR 0016](0016-one-agent-container-per-turn.md)). Hermes has an approval gate of its own, made for a person at a terminal: it flags commands it deems dangerous and asks before running them. In a one-shot chat there is nobody to ask, and Hermes 0.21.6 then applies `approvals.single_query_mode`, `deny` by default: the command is refused, and the agent is told to find another way.

What Hermes flags covers ordinary work: `python3 -c`, a here-document, `execute_code` (a Python script Hermes runs for the agent). In a real run of the Owner side, Hermes refused these one after the other ("BLOCKED: Command flagged as dangerous (script execution via -e/-c flag | via heredoc) but single-query mode (-q) runs without a user present to approve it"): the Owner worked 46 minutes and made 180 calls to the model without producing anything. The seed configuration of the image did not set `approvals`.

Hermes has more gates of the same kind: a write by its file tools to `AGENTS.md`, `CLAUDE.md`, `SOUL.md` or `.cursorrules` always asks a person, even with approvals off, and fails without one; a write to an SSH client configuration asks; a plugin can ask for any tool.

None of them protects anything here. Zephyros does not rest a guarantee on the agent declining ([ADR 0002](0002-guarantees-from-os-and-hub.md)): the attack suite runs the obedient stub, a model that does whatever its context says, and every guarantee holds against it.

## Decision

**Inside its instance, the agent has every power.** Hermes neither asks nor refuses. The managed scope of the instance image (`images/instance/managed.yaml`, `/etc/hermes/config.yaml` in the read-only root, which Hermes lays over every other scope, so the agent's own configuration cannot set these keys back) holds:

- `approvals.mode: "off"`, what `--yolo` does: no gate asks, for a command, for `execute_code`, for a write of the file tools or for a plugin. `"off"` is quoted: bare, YAML reads it as false.
- `approvals.single_query_mode`, `unattended_mode` and `cron_mode` at `approve`: the context of a turn (one-shot chat), and the two others Hermes knows, resolve to running the action should anything read them.
- `approvals.deny: []`. A deny rule matches the text of a command, so it stops nothing the agent wants done (another spelling, a script, `execute_code` reach the same end), and every refusal costs a call to the model. No strong reason for a rule was found.
- `security.protected_instruction_files: false`: the agent can write the `AGENTS.md` or `CLAUDE.md` of the project it builds.

**Why it is safe: what protects is around the agent, not in it.** Each of these holds whatever the agent runs, and none of them is a setting of Hermes:

- the network guard of the instance and its level ([ADR 0015](0015-per-instance-networks-with-a-network-guard.md), [ADR 0024](0024-three-levels-of-network-access.md)): the hub, and for a regulated instance the allowed hosts through the egress proxy of the hub;
- no key in the instance: the model is reached through the gateway of the hub with the project token ([ADR 0008](0008-model-gateway-in-hub.md));
- the container template: user 10000, no capability, a read-only root, the two volumes of the instance, limits of memory, processes and CPU, one container per turn ([ADR 0014](0014-container-filter-as-typed-broker.md), [ADR 0016](0016-one-agent-container-per-turn.md)); the tools of the project read-only ([ADR 0030](0030-the-tools-of-a-project-are-mounted-read-only.md));
- the hub's rights, the same for every tool the agent calls: a Contractor can neither write the contract nor change an acceptance test;
- the gate: the acceptance tests run in a sandbox of their own, from a kit the delivery cannot forge ([ADR 0005](0005-deterministic-acceptance-gate.md), [ADR 0017](0017-verification-runner-and-kits.md)).

**What does not change.** The guarantees above, and what the instance is. A turn could already do all of this: the terminal ran any command Hermes did not flag, and a flagged one could be rewritten so that it was not. The approvals only made work fail. OpenCode needed no change: its managed configuration has allowed every tool without asking since [ADR 0029](0029-the-contractor-can-be-a-third-party-coding-agent.md) (`"permission": "allow"`), and a session of `opencode run` denies only its `question` tool, which nobody could answer.

**What Hermes still refuses.** Some refusals of Hermes are in its code, not its configuration, and stay: a few catastrophic commands (a recursive delete of `/`, of a system directory or of the home, formatting a file system, a fork bomb, killing every process, stopping the machine), the deletion of its own interpreter, and a write by its file tools to its own configuration file or to a system path. They cost the work nothing, and the shell does the last two where the user of the instance may write.

**The test.** `tests/runtime/test_reasoning_and_approvals.py` runs a whole Zephyros, with the obedient stub as the model, and a Hermes turn that runs `python3 -c`, a here-document and `execute_code`: each one leaves its file on the work volume, and no tool result says BLOCKED. `tests/unit/test_instance_image.py` holds the managed scope to these settings.

## Consequences

- Positive: an Owner or a Contractor on Hermes can run the commands its work needs: a script, a test, a here-document.
- Positive: no call to the model is spent on a refusal and on finding another way round it.
- Negative: an agent misled by what it reads (a prompt injection in a delivery, a page it fetches) runs what it was told without a question. That was already true of every command Hermes did not flag; what limits the harm is the container, the guard and the hub, which the attack suite tests with an agent that obeys everything.
- Negative: a new version of Hermes may add a gate, or a key to an existing one. The runtime test fails when a turn is refused again; the managed scope is to be read again with each new version.

## Alternatives considered

- **`single_query_mode: approve` alone**: enough for the commands and `execute_code` in a one-shot chat, not for the writes to instruction files, which ask even then, nor for a gate that a later version reads in another context. `mode: "off"` turns every gate off at once, and the three context modes say the same should a gate read them.
- **Approvals in the seed configuration** (`$HERMES_HOME/config.yaml`): the agent can rewrite it during a turn, and Hermes reads the approvals again at each check. The managed scope cannot be rewritten.
- **A list of deny rules** (`git push --force`, `rm -rf /work`): text patterns that another spelling avoids, for actions the guard, the hub and the gate already make harmless or the agent may need.
- **A terminal backend of Hermes in its own container** (Hermes skips its gates for a Docker backend): a container inside the instance, with access to a container runtime, which the template rules out.
