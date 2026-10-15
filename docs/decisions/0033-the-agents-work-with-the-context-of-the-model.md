# 0033. The agents work with the context of the model

- Status: Accepted
- Date: 2026-10-15
- Decided by the project owner on 2026-10-15 for the direction: the Owner and Contractor agents work with the largest context of the model. For a model of a million tokens, it may go down to 400 000 or 256 000, never below. The design below is the coordinating agent's.
- Extends: [0016](0016-one-agent-container-per-turn.md), [0029](0029-the-contractor-can-be-a-third-party-coding-agent.md)

## Context

An instance continues its conversation from turn to turn ([ADR 0016](0016-one-agent-container-per-turn.md)), and its engine compacts it once it grows too long: it replaces the older part with a summary. Until now the images fixed where. Hermes compacted at 80 000 tokens (`compression.threshold_tokens`). OpenCode compacted near 80 000 too (a context of 96 000 less 16 000 for the answer). Both figures were chosen after the third real run, whose Contractor reached 188 000 tokens a call, each call slower than the last.

In a later real run, the Owner was compacted 14 times in 46 minutes and lost the thread of its work. What it had read, decided and tried was a summary of a summary. The model behind the gateway had a far larger window, which the agents never used.

Neither engine can know that window. Behind the gateway the model has the name the configuration gives it (`gateway.model`), which no catalogue knows: Hermes then assumes 256 000 tokens, and OpenCode takes what its configuration says.

## Decision

**The operator declares the window of the model, and the agents work with all of it.** `gateway.context_window` is the window of the model the gateway serves, in tokens, as its provider gives it (64 000 at least, the smallest Hermes starts with; 256 000 when unset). `gateway.context_tokens` gives the agents less of it, when the operator wants to (for a model of a million tokens: 400 000 or 256 000). Unset, it is the whole window, and it can never be more.

**One compaction point, early enough and no earlier.** Both engines compact at `min(context_tokens, context_window − margin)`. The margin is a tenth of the window, 32 000 tokens at least: it holds the answer of the model and the results of the tools it called before the next check, so that the provider never refuses a call for its length. Each turn carries the window and the point (`ZEPHYROS_CONTEXT_WINDOW`, `ZEPHYROS_COMPACTION_TOKENS`, agent engine interface 0.3.0):

- Hermes, in the managed scope: `model.context_length` is the window, `compression.threshold_tokens` the point, and the ratio `compression.threshold` is 0.95, so that the count decides.
- OpenCode, in its managed configuration: `limit.context` is the window. `limit.input` is the point plus the 16 000 tokens OpenCode keeps for the answer, since OpenCode compacts when a call reaches its input limit less that reserve.

**Below 256 000 tokens: accepted, and said everywhere.** A compaction point below the floor is the operator's choice, or the model's limit; a window the file does not declare, for a real model, is a guess (256 000, so a compaction at 224 000, itself below the floor). The hub accepts both and says so where it cannot be missed:

- `zephyros config check`, which `scripts/deploy.sh up` runs before it starts anything, prints the warnings;
- the hub writes them on its standard error at every start, and records the context in its log (`context.check`, with `context_declared`, `below_floor` and the warnings);
- each start of a turn records the context (`turn.start`: `context_window`, `context_tokens`, `compaction_tokens`), and the evidence report of a delivery gives the context of the turns before it. It opens with CONTEXT BELOW THE FLOOR when one of them compacted below 256 000.

Each says why: below 256 000 tokens, a long task loses the thread of its work, as compactions repeat and the agent re-reads what it already knew. With the stub there is no model, and nothing is said.

When the window itself is below the floor, the agents work with it whole, less the margin, and the warning says that the model is the limit.

**The gateway takes a conversation as large as the window.** Its limits had been set when the engines compacted at 80 000 tokens: 1 MiB of body, 1 000 000 characters and 1 000 messages, which a conversation of about 250 000 tokens passes. They now follow `gateway.context_window`. A request may hold 8 bytes per token of the window plus 1 MiB for the system prompt and the tool definitions, in body and in characters, and one message per 64 tokens of the window (1 000 at least). The 8 bytes are twice what was measured on the JSON of a real conversation: 152 calls of a Contractor, tool calls and their results, at 3.9 bytes a token (median error 0.7 %, against the tokens the provider counted). The system prompt and the 25 tools of Hermes measured 55 000 bytes, 4.3 bytes a token. A conversation at the compaction point therefore passes with room to spare.

**What the turn got is in the journal of the filter.** The hub's log records the context and the session the hub meant (`turn.start`). The filter reads the same configuration on its own, and builds the turn from it. Its journal records, for each turn it starts, the session, the window, the compaction point and the reasoning flag it gave the turn, so a difference between the two configurations shows.

## Consequences

- Positive: the agents keep what they read and decided for as long as the model can hold it, and compact once a session nears the end of the window, not every few minutes.
- Cost: each call sends the whole conversation, so a long context means more input tokens a call. Providers with a prompt cache (DeepSeek among them) bill the part they have seen before at a fraction of the price, and a turn that appends to its conversation is mostly that part. Compaction breaks the cache: the summary is new text, billed in full, and so is everything after it. Fewer compactions also mean fewer calls spent on writing summaries.
- Latency: a call with a long conversation takes longer before the first token, more so when the cache misses. The hub's limits stay as they are (`gateway.timeout_seconds`, `limits.turn_seconds`).
- Negative: the window is declared, not measured. A window declared larger than the model's leads to calls the provider refuses once the conversation passes the real window. The provider's reason appears in the log of the hub (`gateway.call`).
- Negative: the margin is a rule, not a measure. A tool that returns more than a tenth of the window in one call can still overflow; the engines then compact on the refusal, if they do.
- Negative: what the gateway takes stays bounded. A request is never more than 16 MiB, whatever the window: parsing one takes about twice its size, and the hub (512 MiB) may serve several instances at once. That covers a window of about two million tokens. Past it, the gateway refuses a larger request (413), which the engine reports as an error; give the agents less with `context_tokens`. The answer of the provider is not bounded, as before.

## Alternatives considered

- **Keep 80 000 and raise it by hand in the images**: the window belongs to the model the operator chooses, not to the image.
- **Ask the provider for the window**: OpenAI-compatible providers do not all say it, and the gateway would have to pass it through under its own name; a declared value is one line of the configuration.
- **Refuse a context below 256 000**: a model with a smaller window could not run at all. Accepted with a warning, the operator decides knowingly.
- **Compact at the context the operator gives, without a margin**: with the whole window given, the next answer would overflow it.
