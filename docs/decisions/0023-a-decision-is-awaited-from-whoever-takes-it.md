# 0023. A decision on the contract is awaited from whoever is to take it

- Status: Proposed
- Date: 2026-10-14
- Supersedes: the decision task of the Owner side as [0021](0021-the-ladder-waits-for-a-ready-contract.md) left it ("The Owner side's decision task is unchanged", and the consequences that follow from it)

## Context
ADR 0021 held the Contractor's task while a change of the contract awaits a decision. It left
the Owner side's decision task as it was: every `contract.propose` created it anew, carried by
the Owner side and counted from the proposal, and any decision of the project ended it. The
review of that change found three faults.

- An Owner side that proposes more often than `continuity.inactivity_seconds` keeps both
  ladders silent: each proposal starts the decision task again, and holds the Contractor's.
  That is what a model in a loop does, and what a hostile instance can do. Before ADR 0021, the
  Contractor's ladder at least ended by alerting the main agent.
- The proposer of a change never approves it. A change the Owner side proposed awaits the
  operator or the main agent, yet the ladder woke and restarted the Owner side for it: turns
  spent on a change it cannot approve, which may propose again.
- Any decision ended the task, even when another change could still be approved. A version
  written by the operator (`contract.apply_reserved`, `contract.import`) ended nothing, though
  no pending change could be approved any more.

## Decision
- The decision task follows the contract, not the calls. Each time a version or a pending change
  comes or goes, the hub derives it from the pending changes. It exists while a change that
  could still be approved awaits a decision (one proposed on the version in force), and it is
  about the oldest of them.
- Whoever is to decide that change carries the task: the Owner side for a change of the operator
  or the main agent, as before; the main agent for a change of the Owner side. The main agent
  has no instance to wake, so the ladder of its decision has two steps: `continuity.alert_main`
  after `continuity.inactivity_seconds`, then `continuity.alert_human` after
  `continuity.unblock_seconds`. The Owner side's calls are not activity on that task.
- Only a decision starts the decision ladder again; a later proposal leaves it where it is.
  When the oldest change is decided and another remains, the task moves to that one, counted
  from the decision. When none remains, the task ends.
- The Contractor's task keeps ADR 0021: it is held while a change awaits a decision. A release
  starts its ladder from the wake because the task could not be carried while it was held, not
  because the contract changed. A version that comes while the Contractor carries its task
  leaves its ladder where it is.

## Consequences
- Positive: an Owner side that proposes without end while nobody decides reaches the main agent
  after `continuity.inactivity_seconds`, then the human. No Owner turn is spent on a change it
  cannot approve. While the work waits for a decision, one ladder always runs.
- Positive: a version that leaves no change to approve ends the decision task, whoever wrote it.
- Negative: the main agent's calls do not count as activity: it is alerted when the step is due,
  even while it reads the change. An alert is a notification, not a turn.
- Negative: an Owner side that proposes, then rejects its own change, again and again, ends the
  task each time, and nothing escalates within the turn. The limit of a turn
  (`limits.turn_seconds`) ends such a loop; then the ladder of the Contractor, or of the
  decision, runs. The hub counts no streak of proposals.
- Negative: a decision the Owner side is to take is carried as before. Its calls count as
  activity, so an Owner side that keeps calling without deciding escalates one step per turn,
  each turn ended by its limit.

## Alternatives considered
- Keep the Owner side as the carrier and only stop resetting the task on a proposal. The Owner
  side would still be woken and restarted for a change it cannot approve, with turns that may
  propose again.
- Count a streak of proposals without a decision, as EXG-23 counts refused deliveries. It needs
  a threshold, an event and a rule to end the streak, for a case the limit of a turn already
  bounds.
- Release the Contractor while a change of the Owner side awaits the main agent. The Contractor
  would build against a contract about to change: the cost ADR 0021 removed.
