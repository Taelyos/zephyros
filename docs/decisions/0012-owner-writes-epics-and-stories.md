# 0012. Epics and user stories are written by the owner side; the contractor owns the solution
- Status: Accepted
- Date: 2026-10-09

## Context
When the contractor writes the epics and stories, they drift toward least effort.

## Decision
The owner side writes the epics and user stories, and the contractor owns the solution. Work runs in four phases: preparation (owner and human write the need analysis, the contract, epics, stories and acceptance test plan), kickoff (contractor questions; owner validates the design document), delivery (increments, demos, verification) and acceptance. Contract changes fall into three classes: clarification (owner alone), minor improvement (owner alone, human notified) and amendment (human signs). The hub forces any change touching a contractual requirement or a P0 criterion into the amendment class.

## Consequences
- Positive: the owner keeps control of requirements, so the contractor cannot quietly lower the bar.
- Positive: changes are classed by impact; anything material needs the human's signature.
- Negative: preparation demands more upfront work from the owner and the human.
- Negative: the hub's classification rules are critical; an error could misfile a change.

## Alternatives considered
- The contractor writes epics and stories, as in classic subcontracting.
