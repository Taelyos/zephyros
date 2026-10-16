# 0034. A blocked agent reports an obstacle, and decisions take it up a chain

- Status: Accepted
- Date: 2026-10-16
- Decided by the project owner on 2026-10-16 for the direction: a chain of escalation, from the Contractor to the Owner side, from the Owner side to the operator (the main agent), from the operator to the human, each step decided by whoever holds the obstacle. The design below is the coordinating agent's.
- Extends: [0016](0016-one-agent-container-per-turn.md) (a turn per container, woken by the hub), [0020](0020-the-owner-sees-and-records.md) (the Owner side sees and records), [0023](0023-a-decision-is-awaited-from-whoever-takes-it.md) (a task carried by whoever decides)

## Context

In a real run, the Contractor spent 90 minutes against a defect of its environment: a tool of the project announced an action under way that could never complete. It said so in the summary that ends its turn, which nobody read. No channel of Zephyros carried a limitation from an agent to anyone who could remove it. An agent that meets what it cannot change could only keep trying or give up.

The owner asked for a chain: the Contractor tells the Owner side, which may tell the operator (the main agent), which may tell the human.

## Decision

**An obstacle is a piece of evidence, bounded; not a conversation.** An instance reports one with `obstacle.report`:

| Field | What it holds | Bound |
| --- | --- | --- |
| `nature` | `environment`, `tool`, `contract`, `acceptance_test`, `budget` or `other` | |
| `blocking` | whether the agent cannot go on with its task until it is answered | |
| `title` | one line | 120 characters |
| `description` | what happens, and what it keeps the agent from doing | 4 000 |
| `evidence` | 1 to 10 references: a path under `/work` or `/opt/project-tools`; a command, the end of its output and its exit code; a delivery of the project; a record of the log of the project | 500 for a command, 2 000 for its output, 300 for a note |
| `tried` | what was tried, and what came of it | 2 000 |
| `needed` | what would remove the obstacle | 1 000 |

Each item of evidence has the fields of its kind and no other. The hub checks that a delivery or a record of the log it names exists in the project; it never reads a path: an obstacle names evidence, it does not reach it. Obstacles are numbered per project, `OB-001`, `OB-002`, in the order they are reported.

**The chain, one decision at each step.** An obstacle awaits an answer from one level at a time:

| Level | Awaits | Who answers | Decisions |
| --- | --- | --- | --- |
| `owner` | an obstacle of the Contractor | the Owner instance of the project; the operator | `clarify` (the need, made precise), `amend` (a change of the contract, `change_id`, pending or approved), `fix_test` (acceptance tests the hub keeps, `paths`), `reject` (with the reasons: a fault of the delivery, a question of how to build), `escalate` |
| `operator` | an obstacle of the Owner side, or one escalated | the operator, the main agent | the same, `handle` (`action`: `relaunched`, `new_session`, `stopped`, `environment_fixed`, `other`), and `escalate` |
| `human` | an obstacle escalated by the operator or the main agent | the operator channel, the human's own | the same, without `escalate` |

Its life: `open`, then `answered`, `rejected` or `escalated` (which awaits the next level), then `closed`, by its author (once answered, or to withdraw it) or by the operator. Nobody answers an obstacle of their own. `handle` records what the operator or the main agent did with the tools it already has (`instance.start`, with `session: new`; `instance.stop`): the answer does not act. `amend` and `fix_test` are checked for existence only: the change is one of the project and was not rejected, the paths are acceptance tests the hub keeps. Nothing ties the change or the test to the obstacle; the text of the answer says how they answer it, and the reader judges.

**Who reads what.** The rights matrix ([docs/tools.md](../tools.md)) says who may use each tool, deny by default: `obstacle.report` the instances; `obstacle.list` and `obstacle.read` every principal, each in its scope; `obstacle.answer` the Owner instance, the operator and the main agent; `obstacle.close` the instances, for their own obstacles, and the operator. Which obstacles a principal reads, answers or closes follows who they concern (`core/obstacles.py`): the Contractor reads its own obstacles only, never one of the Owner side, which may speak of the acceptance tests it keeps hidden; the Owner side reads its own and the Contractor's; the operator and the main agent read every project's. An instance of another project is refused (`other_project`), as for every tool. The Contractor told `forbidden` on a number learns that an obstacle of the Owner side has it, which the evidence report counts anyway; a piece of evidence of the kind `log` tells it whether a record of that number exists in the log of its project.

**An obstacle is data, never an instruction.** Its title, description, evidence and answers are an agent's text, and the other side reads it: the Owner side the Contractor's, the main agent and the operator the Owner side's and what was escalated. A blocking obstacle wakes the Owner side, which can then propose a change of the contract and write the acceptance tests. The base instruction of the images, the descriptions of the tools and the documentation of the operator say it: what an obstacle says is weighed as data, checked against the contract, the tests and the delivery, never followed as an instruction. The line the hub adds to the prompt of a turn holds only identifiers and the hub's own words.

**No secret is kept.** Every free text of an obstacle, the evidence included, is masked as it is written (EXG-49): the secrets the hub knows (the key of the provider, the token of the forge) and whatever is shaped like a token. The output of `env` given as evidence is stored, read and logged with its tokens masked.

**A blocking obstacle is a task of the continuity, carried by whoever is to answer it.**

- `obstacle`: the oldest blocking obstacle of the Contractor that awaits the Owner side. The Owner side carries it, and its ladder is the Owner's: wake, the restarts, the main agent, the human. **The wake is due at once**: the Contractor is blocked now. An Owner side busy with a turn is woken as soon as that turn ends.
- `obstacle_raised`: the oldest blocking obstacle that awaits the operator. The main agent carries it, as it carries a decision on a change of the Owner side ([ADR 0023](0023-a-decision-is-awaited-from-whoever-takes-it.md)): `continuity.alert_main` after `continuity.inactivity_seconds`, then `continuity.alert_human` after `continuity.unblock_seconds`.
- **Only an answer counts on these tasks.** Another call of the side that carries one is not activity on it: an Owner side that keeps working without answering is still woken. When the obstacle is answered, escalated or closed, the task moves to the next blocking one, or ends. An obstacle that reaches the human has no ladder: the escalation is the alert.

The tasks are derived from the obstacles each time one changes, and again when the hub starts, like the decision task.

**The main agent hears of what reaches the operator or the human.** The event `obstacle.raised` (events schema 2.1.0) goes out, signed, when an obstacle reaches the operator (the Owner side reports one, or escalates one of the Contractor's) and when it reaches the human: `project_id`, `obstacle_id`, `side`, `nature`, `blocking`, `title`, `level` (`operator` or `human`), `by`. Below the operator, the chain stays between the two sides: a report of the Contractor reaches the main agent only when the Owner side escalates it. What awaits the human is a state anyone with the operator token follows without reading the log: `obstacle.list` with `status: awaiting, level: human`, across every project. The human has no receiver of their own: the event at the level `human` goes to the receiver of the main agent, like every notification (see below for a hook of the human's own).

**The author learns of the answer at its next turn.** When a turn starts, the hub writes after its prompt one paragraph: the obstacles of the side on which a decision was taken since it was last told, and, for the Owner side, the obstacles that await its answer. The record `turn.start` names them (`obstacles`). The author reads the decisions with `obstacle.read`, which marks them seen. A prompt that leaves no room within its 4 000 characters goes alone: nothing of it is cut, and the side is told at a later turn.

**No answer shows the Contractor a hidden test.** The Contractor reads every answer to its obstacles, the reasons of an escalation included. The hub refuses (`acceptance_leak`) an answer, or a note of the operator closing it, that copies a passage of an acceptance test the hub keeps for the Owner side. Both texts are compared without the marks a quote (`> `), a numbered listing (`12: `, `12|`, the tab of `cat -n`) or a diff (`+`, `-`) puts before a line, whitespace aside: windows of 120 characters of the answer, taken every 40 characters, are looked for in each file, and a file shorter than 120 characters is looked for whole. A copied passage of 160 characters or more is always found, and so is a short file of 40 characters or more copied whole; a passage between 120 and 160 characters may be. Nothing is written when an answer is refused. What this does not catch:

- a file under 40 characters, and a passage under 160 characters of a longer file, which may pass;
- a rephrasing, a passage split, re-encoded, marked otherwise (as a comment, in HTML) or spread over several answers, a value without its context (`0.87`);
- an earlier version of a test: only the version the hub keeps now is compared, and the hub keeps no other, since `acceptance.put` overwrites ([ADR 0028](0028-the-contract-of-a-project-is-stored-in-sqlite.md)). The version a `fix_test` replaced, the one the Owner side is most likely to quote, is gone. Should the hub keep the versions of a test (the second option of ADR 0028), the check is to cover them;
- the output of a run, which the Owner side reads in the whole evidence report: a pasted output passes unless it quotes 160 characters of a test.

It closes the accident of an Owner side that pastes its test to explain it; it is no barrier against an Owner side that means to tell. The obstacles of the Owner side are never read by the Contractor, and are not checked: the Owner side may quote its tests to the operator.

**Bounds, and what an obstacle does not do.**

- At most 5 obstacles of a side await an answer at once, and a side reports at most 3 in a turn (counted from the start of the current turn of the side, or over the last hour when the hub knows no turn): `too_many_obstacles`. Withdrawing an obstacle does not give the turn one more.
- It moves no deadline. A report is a call like any other: it holds neither the task of the Contractor nor its ladder ([ADR 0021](0021-the-ladder-waits-for-a-ready-contract.md)), and changes neither `limits.turn_seconds` nor `limits.silence_seconds`.
- It reaches nothing new: the tools store and read records of the hub, and no tool of the instance, no file, no container and no forge is touched. The obstacles are kept in the database of the hub (table `obstacles`) and outlive a restart.

**Visible where the rest is.** Each call is one record of the log (`obstacle.report`, `obstacle.list`, `obstacle.read`, `obstacle.answer`, `obstacle.close`), in the chain of hashes, with what an exporter of the thread of a run needs and no free text but the title: `log.read` shows them, and the replay format gives them a category of their own (`obstacle`, 2.2.0). The evidence report of each delivery has a section "Obstacles": every obstacle of the project when the report was written, its side, nature, state and the decisions taken on it (the decision and who took it, never the texts). The report the Contractor reads leaves out those of the Owner side and counts them. The report is the body of the pull request: the titles and decisions go to the forge with it, masked as the rest of it is.

**The agents know it.** Each engine reads a base instruction from the read-only root of its image (`instructions.md`, the same text in both): Hermes as an ephemeral system prompt (`HERMES_EPHEMERAL_SYSTEM_PROMPT`, which the entrypoint sets), OpenCode through `instructions` in its managed configuration. It says when to report an obstacle (after a real attempt, about what the agent cannot change; never to ask how to build, nor about a fault of its own work), what to put in it, who answers, and, for the Owner side, how to answer without copying a test. The descriptions of the five tools say the same where the model reads them.

## Consequences

- Positive: a blocked agent has a third way between insisting and giving up, and the hub tells the one who can act. An Owner side that does not answer is woken, then restarted, then reported to the main agent and to the human.
- Positive: each step is a decision, logged and readable; the human sees what reached them without reading the log.
- Positive: the evidence report says what stood in the way of a delivery, and how it was answered.
- Negative: five tools more on the surface of the instances. Each stores or reads records of the hub, in the scope of the project, and is logged; the attacks are in `tests/acceptance/test_obstacles.py` (the Contractor reading an obstacle of the Owner side, an instance of another project, an answer that copies a hidden test).
- Negative: the leak check is a check of copies, not of meaning, and of the current version of each test only (above).
- Negative: the text of an obstacle is an agent's, up to some 9 000 characters with its evidence, and it reaches the other side: an Owner side woken by a blocking obstacle reads what the Contractor wrote, and may then propose a change of the contract or write a test. The base instruction and the descriptions of the tools say to weigh it as data; nothing enforces it, as nothing enforces what any agent makes of what it reads ([ADR 0002](0002-guarantees-from-os-and-hub.md)). What enforces is around the agent: a change of the contract still awaits the operator or the main agent, and the gate still runs the tests the hub keeps.
- Negative: an Owner side woken for an obstacle spends a turn on it, and its restarts are restarts of the instance. A Contractor that waits for an answer is still woken by the ladder of its delivery: an obstacle moves no deadline, by decision.
- Negative: the base instruction is guidance, never a guarantee ([ADR 0002](0002-guarantees-from-os-and-hub.md)). Hermes reads `.env` of its home over its environment, so an agent can set the instruction aside for its later turns by writing one; it loses only its own guidance. It adds about 600 tokens to the system prompt of each call.
- The surfaces move: events schema 2.1.0 (`obstacle.raised`, and the tasks `obstacle` and `obstacle_raised` in the alerts of the continuity), replay format 2.2.0 (the category `obstacle`), two refusal codes (`too_many_obstacles`, `acceptance_leak`). The configuration does not change: the bounds are fixed.

## Proposed, not built: a hook for the human

An obstacle that reaches the human is an event of the notifier, which goes to the main agent. A human who wants it elsewhere — a phone, a chat room — has no hook of their own. A second destination, `[notifications.human]` with its own `url` and `secret`, would receive the same signed delivery for `obstacle.raised` at the level `human` and for `continuity.alert_human`, through the same queue, the same signature and the same egress policy (its host joins the allowed ones, as the first receiver's does). It is simple and safe in that it adds no new kind of sending; it is left out of this change because no human receiver exists to try it on.

## Alternatives considered

- **A free conversation between the sides** (messages, a thread): what the owner ruled out. It has no bound, invites the Contractor to ask how to build, and is a channel from the Owner side's hidden tests to the Contractor that a copy check cannot close.
- **Reading the summary that ends a turn**: the hub would parse what a model says; the summary has no structure, no evidence and no addressee.
- **An obstacle that holds the deadlines** (no ladder for the Contractor while it waits): an agent could stop its own clock by reporting. The owner asked for none.
- **Waking the Owner side only after `continuity.inactivity_seconds`**, as for a delivery: the Contractor is blocked from the moment it reports; the wait would be pure loss.
- **Escalation to the main agent at every report of the Contractor**: the Owner side is the first to answer what the contract says and what its tests ask, and most obstacles are its own to answer.
- **The texts of the answers in the evidence report**: they may speak of the tests, and the report is the body of the pull request. The decisions and who took them are there; the texts stay in the hub.
- **A semantic check of the answers** (a model judging whether an answer reveals a test): a judgment by a model inside the hub, which has none ([ADR 0003](0003-owner-and-contractor-instances-with-hub.md)), and no more reliable.
