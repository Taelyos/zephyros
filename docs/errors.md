# Error messages

Each error meant for the user carries three items (EXG-51, annex "Error message items"): a
stable error code (the `reason` of the log record and of the answer), the cause of the error
(the message), and the documented corrective action below. Where each answer carries them:

| Where | What a refusal answers |
| --- | --- |
| Operator channel | `error.kind`, `error.reason` (the code), `error.message` and `error.action` |
| `/mcp`, on the agent endpoint and the main-agent endpoint | A tool result with `isError: true`, whose text is a JSON object: `error` (the kind), `reason` (the code), `message` and `action`. An unknown tool is a JSON-RPC error instead (`-32602`, "unknown tool") |
| `/staging/...` on the agent endpoint | `error` (the message) and `action` |
| `/v1/chat/completions` (the model gateway) | The format of the OpenAI API, which model clients read: `error.message`, `error.type` and `error.code`, a code of the gateway listed [below](#codes-of-the-model-gateway). No `action` field |

Some answers carry less, on purpose:

- A failed authentication is the same `401 {"error": "unauthorized"}` wherever it happens, so
  that it tells an attacker nothing. The log records why.
- A body that cannot be read (not JSON, too large, a malformed delivery identifier) is answered
  with `error` alone, before any tool runs. The log records it as `invalid_request`.
- Every refusal of the gateway is also logged with its refusal code (the `reason` of the log
  record), which this table documents.

This file is kept in step with `Reason` and `REMEDIES` by `tests/unit/core/test_errors.py`.

| Code | Corrective action |
| --- | --- |
| `none` | Nothing to do: the action succeeded. |
| `unauthenticated` | Send a token: of an instance, the operator, or the main agent. |
| `unknown_token` | Ask the operator for a fresh token; the presented one is not known. |
| `wrong_source` | Call from the address the token was issued to (EXG-60). |
| `expired` | The token has expired: ask the operator for a fresh one. |
| `revoked` | The token was revoked, as when an instance stops: ask for a fresh one. |
| `wrong_endpoint` | Use the endpoint of this caller: operator, main agent, or agent. |
| `forbidden` | Check what this caller may do in docs/tools.md, or change the caller. |
| `other_project` | Use a token of the project the object belongs to. |
| `unknown_action` | Use a tool name of docs/tools.md; the MCP tools/list names them. |
| `invalid_request` | Fix the arguments the message names; the schema rejects them. |
| `not_found` | Check the identifier: the object does not exist in this project. |
| `self_approval` | Another principal must approve: propose, then let the Owner approve. |
| `reserved_data` | Operator-reserved data changes with contract.apply_reserved only. |
| `not_pending` | The change is no longer pending: read the contract, propose again. |
| `stale_base` | Re-read the contract: the change was written against an older version. |
| `browser_request` | Call the hub with a tool, never from a browser page. |
| `bad_host` | Use the host the hub is published on; check the configuration. |
| `unreadable` | Check the file or the reference: it could not be read. |
| `busy` | Retry once the work in flight has finished. |
| `protected_path` | The path belongs to the acceptance material; deliver elsewhere. |
| `egress` | Name the host in the configuration; the hub reaches no other (EXG-48). |
| `rate_limited` | Wait a minute: this instance sent too many refused actions (ATK-71). |

## Codes of the model gateway

`/v1/chat/completions` answers a refused or failed call with one of these codes in
`error.code`. The gateway serves one model, by the name `GET /v1/models` gives.

| Code | Status | Corrective action |
| --- | --- | --- |
| `bad_body` | 400, 413 or 415 | Send one JSON object (`application/json`) of reasonable size. |
| `forbidden` | 403 | Call with the token of an instance of a project; no other caller uses the gateway. |
| `model_not_found` | 404 | Ask for the model by the name `GET /v1/models` gives. |
| `invalid_request` | 400 | Send an object with a non-empty `messages` list. |
| `invalid_message` | 400 | Give each message an object with a known role (`system`, `developer`, `user`, `assistant`, `tool`). |
| `invalid_content` | 400 | Give each message text content: a string or a list of text parts. |
| `unsupported_content` | 400 | Send text parts only; images and other parts are not served. |
| `invalid_tool_call` | 400 | Give each tool call an `id`, a `type` and a `function` with a name and arguments. |
| `invalid_tools` | 400 | Send `tools` as a list of functions, each with a JSON Schema object as parameters. |
| `too_many_messages` | 400 | Shorten the conversation: the gateway takes a bounded number of messages. |
| `too_large` | 413 | Shorten the conversation: its text is over the gateway's limit. |
| `upstream_failure` | 502 | The provider failed; the log of the hub holds its detail, masked. Retry later. |
