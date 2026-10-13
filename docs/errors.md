# Error messages

Each error meant for the user carries three items (EXG-51, annex "Error message items"): a
stable error code (the `reason` of the log record and of the answer), the cause of the error
(the message), and the documented corrective action below. The operator channel returns them
under `error.kind`, `error.reason`, `error.message` and `error.action`; the agent endpoints
return the message and the action.

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
