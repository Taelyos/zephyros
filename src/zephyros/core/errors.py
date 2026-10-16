"""Errors that cross the layers of the hub."""

from __future__ import annotations

from zephyros.core.audit import Reason


class Refusal(Exception):  # noqa: N818  # reason: a refusal is a decision of the system, not a fault
    """The system refuses an action. `reason` is the code that the log records."""

    def __init__(self, reason: Reason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# EXG-51: every error meant for the user carries a stable code, its cause, and a documented
# corrective action. This table is the third item; docs/errors.md is its readable form, and a
# test keeps the two in step with the codes of `Reason`.
REMEDIES: dict[Reason, str] = {
    Reason.NONE: "Nothing to do: the action succeeded.",
    Reason.UNAUTHENTICATED: "Send a token: of an instance, the operator, or the main agent.",
    Reason.UNKNOWN_TOKEN: "Ask the operator for a fresh token; the presented one is not known.",
    Reason.WRONG_SOURCE: "Call from the address the token was issued to (EXG-60).",
    Reason.EXPIRED: "The token has expired: ask the operator for a fresh one.",
    Reason.REVOKED: "The token was revoked, as when an instance stops: ask for a fresh one.",
    Reason.WRONG_ENDPOINT: "Use the endpoint of this caller: operator, main agent, or agent.",
    Reason.FORBIDDEN: "Check what this caller may do in docs/tools.md, or change the caller.",
    Reason.OTHER_PROJECT: "Use a token of the project the object belongs to.",
    Reason.UNKNOWN_ACTION: "Use a tool name of docs/tools.md; the MCP tools/list names them.",
    Reason.INVALID_REQUEST: "Fix the arguments the message names; the schema rejects them.",
    Reason.NOT_FOUND: "Check the identifier: the object does not exist in this project.",
    Reason.SELF_APPROVAL: "Another principal must approve: propose, then let the Owner approve.",
    Reason.RESERVED_DATA: "Operator-reserved data changes with contract.apply_reserved only.",
    Reason.NOT_PENDING: "The change is no longer pending: read the contract, propose again.",
    Reason.STALE_BASE: "Re-read the contract: the change was written against an older version.",
    Reason.BROWSER_REQUEST: "Call the hub with a tool, never from a browser page.",
    Reason.BAD_HOST: "Use the host the hub is published on; check the configuration.",
    Reason.UNREADABLE: "Check the file or the reference: it could not be read.",
    Reason.BUSY: "Retry once the work in flight has finished.",
    Reason.PROTECTED_PATH: "The path belongs to the acceptance material; deliver elsewhere.",
    Reason.EGRESS: "Name the host in the configuration; the hub reaches no other (EXG-48).",
    Reason.RATE_LIMITED: "Wait a minute: this instance sent too many refused actions (ATK-71).",
    Reason.TOO_MANY_OBSTACLES: (
        "Wait for an answer, or close an obstacle that no longer stands: a side has at most 5 "
        "awaiting an answer, and reports 3 in a turn."
    ),
    Reason.ACCEPTANCE_LEAK: (
        "Say in your own words what the acceptance test expects: the Contractor reads the "
        "answer, never the test."
    ),
}


def remedy(reason: Reason) -> str:
    """The documented corrective action for a refusal code (EXG-51)."""
    return REMEDIES[reason]
