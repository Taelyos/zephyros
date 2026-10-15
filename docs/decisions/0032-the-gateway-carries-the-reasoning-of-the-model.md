# 0032. The gateway carries the reasoning of the model, and the configuration says whether it goes back

- Status: Accepted
- Date: 2026-10-15
- Decided by the project owner on 2026-10-15 for the direction: the reasoning of DeepSeek goes back whatever the model is named behind the gateway; nothing rests on that name. The design below is the coordinating agent's.
- Extends: [0008](0008-model-gateway-in-hub.md), [0029](0029-the-contractor-can-be-a-third-party-coding-agent.md)

## Context

A thinking model gives its reasoning with each answer, in a field of its own (`reasoning_content`, beside the content and the tool calls). Providers disagree on what comes next:

- DeepSeek in thinking mode wants it back. A conversation whose assistant message with tool calls comes without its `reasoning_content` is refused (HTTP 400, "The `reasoning_content` in the thinking mode must be passed back to the API"); one that carries it, even a single space, is accepted. An empty text is refused by some versions.
- A strict OpenAI-compatible provider refuses the field on any message, even empty (HTTP 400 or 422, "Extra inputs are not permitted").

Three things stood in the way in a real run with DeepSeek V4.1 Flash:

1. **The gateway dropped the field both ways.** Its types had no place for it: the reasoning of an answer never reached the instance, and nothing the instance sent back reached the provider.
2. **Hermes decides from the name.** Hermes 0.21.6 sends the reasoning back for the families it knows, by provider, by host, or by the model name holding `deepseek`. Behind the gateway the provider is `custom`, the host is the hub, and the name is `gateway.model` ("served-model" in the examples): Hermes treats the gateway as a strict provider and strips the field. It has an explicit switch, `reasoning_echo` on its `model:` entry.
3. **OpenCode decides from the name too.** OpenCode 1.19.0 sends the reasoning of a model declared in its configuration back in `reasoning_content` when the model's identifier holds `deepseek`; under another name, it sends nothing. It has an explicit switch, the `interleaved` field of the model.

An answer of DeepSeek can come without reasoning (a short answer with two tool calls): the next call then has nothing to send back, and is refused. The Owner turn ended with `failed:exit_1`, the gateway having answered 502.

## Decision

**The gateway carries the reasoning both ways.** `ChatMessage.reasoning` (model provider interface 0.2.0) holds it on an assistant message. The gateway reads `reasoning_content` from an answer and gives it to the instance, whole or streamed (a delta of its own before the tool calls or the content); it reads `reasoning_content` from each assistant message an instance sends and hands it to the provider. A value that is not a text is refused (400), and the reasoning counts in the size of the conversation.

**The provider decides what goes on, from `gateway.reasoning_echo`.** With `true`, the OpenAI-compatible provider sends every assistant message with `reasoning_content`: its reasoning, or a single space when it has none (an answer without reasoning, an engine that dropped it). With `false`, it sends the field on no message. The pad is what makes the run independent of the engine and of the answers: a call is never refused for a missing reasoning.

**Unset, the key follows the model at the provider, never the name the instances ask for.** `true` when `gateway.upstream_model` (or `gateway.model` when that is unset, since it is then what the provider receives) holds `deepseek` or `mimo`, or the host of `gateway.upstream_url` is that of DeepSeek, Kimi (Moonshot) or MiMo; `false` otherwise, and with the stub. These are the families Hermes knows. The operator sets the key when the rule is wrong for a provider: a DeepSeek model served under another name, or one at a strict host.

**Each turn tells its engine to keep the reasoning** (agent engine interface 0.3.0: `TurnRequest.reasoning_echo`, the variable `ZEPHYROS_REASONING_ECHO`, `true` or `false`), so that the provider gets the reasoning itself rather than the pad:

- Hermes: `model.reasoning_echo` in the managed scope of the image. Hermes reads the truth of the text it expands there, where `false` would read as true: the entrypoint turns the variable into `true` or an empty text. With it, Hermes also pads a message without reasoning itself.
- OpenCode: `"interleaved": "{env:ZEPHYROS_REASONING_FIELD}"` on the model of the managed configuration; the entrypoint sets the field to `reasoning_content`, or to an empty text, which keeps nothing and overrides OpenCode's guess from the name.

## Consequences

- Positive: a thinking model that wants its reasoning back works under any name, with either engine, and a strict provider never sees the field.
- Positive: the model reads its own reasoning again in a loop of tool calls, as its provider intends, rather than a pad.
- Negative: the conversation the gateway sends grows by the reasoning of every answer it keeps. Both engines compact the conversation where the configuration says ([ADR 0033](0033-the-agents-work-with-the-context-of-the-model.md)), the reasoning included.
- Negative: the default is a rule on names and hosts, which a new family or a new reseller can defeat; the key is there for that, and the gateway's log names the provider's reason for a refused call.
- Negative: `model.reasoning_echo` and `interleaved` are internals of Hermes 0.21.6 and OpenCode 1.19.0. The runtime test with a provider that demands the field fails when a new version drops them.
- Not shown: a real model through this path. The runtime test runs a fake provider behind the gateway that refuses an assistant message with tool calls without `reasoning_content`, and answers once with two tool calls and no reasoning; a Hermes turn and an OpenCode turn go to their end, and the provider receives back the reasoning it gave.

## Alternatives considered

- **The engines alone**, `reasoning_echo` and `interleaved` set, the gateway untouched: the gateway dropped the field, so nothing reached the provider.
- **The pad alone, in the gateway**: every call accepted, but the model never reads its reasoning again.
- **A name that holds `deepseek`** for `gateway.model`: works for both engines today, ties a guarantee to a guess of each engine, and the operator chooses that name for other reasons.
- **Always send the field**: a strict provider refuses every call.
- **A key per engine** in the configuration: the provider decides what it wants, not the engine; one key says it once.
