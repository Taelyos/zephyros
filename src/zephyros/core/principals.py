"""Who acts on the hub.

A principal is an identifier that the hub issued and that survives restarts: the instance of
a side of a project is the same principal after a restart and after a rotation of its token
(ATK-89). Identity is never read from a request field; it comes from the token record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from zephyros.core.ports.agent_engine import Side

_PROJECT_ID = re.compile(r"^p_[a-z0-9]{6,32}$")
_INSTANCE_NAME = re.compile(r"^[a-z0-9]{1,16}$")
DEFAULT_INSTANCE = "main"


class PrincipalKind(StrEnum):
    OPERATOR = "operator"
    MAIN_AGENT = "main-agent"
    OWNER = "owner"
    CONTRACTOR = "contractor"


def is_project_id(text: str) -> bool:
    return bool(_PROJECT_ID.fullmatch(text))


@dataclass(frozen=True, slots=True)
class Principal:
    """The identity behind a call. For an instance, `id` is `<side>:<project>` (and
    `<side>:<project>:<name>` for an instance other than the first of its side)."""

    id: str
    kind: PrincipalKind
    project_id: str | None = None
    instance_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind in (PrincipalKind.OPERATOR, PrincipalKind.MAIN_AGENT):
            if self.project_id is not None or self.instance_name is not None:
                raise ValueError("the operator and the main agent belong to no project")
            if self.id != self.kind.value:
                raise ValueError("their identifier is their kind")
        else:
            if self.project_id is None or not is_project_id(self.project_id):
                raise ValueError("an instance belongs to a project")
            name = self.instance_name or DEFAULT_INSTANCE
            if not _INSTANCE_NAME.fullmatch(name):
                raise ValueError("an instance name is lower-case letters and digits")
            suffix = "" if name == DEFAULT_INSTANCE else f":{name}"
            if self.id != f"{self.kind.value}:{self.project_id}{suffix}":
                raise ValueError("the identifier of an instance names its side, project and name")

    @property
    def side(self) -> Side | None:
        if self.kind is PrincipalKind.OWNER:
            return Side.OWNER
        if self.kind is PrincipalKind.CONTRACTOR:
            return Side.CONTRACTOR
        return None

    @property
    def is_instance(self) -> bool:
        return self.side is not None


OPERATOR = Principal("operator", PrincipalKind.OPERATOR)
MAIN_AGENT = Principal("main-agent", PrincipalKind.MAIN_AGENT)


def instance_principal(side: Side, project_id: str, name: str = DEFAULT_INSTANCE) -> Principal:
    """The principal of the instance `name` of `side` in `project_id`."""
    kind = PrincipalKind.OWNER if side is Side.OWNER else PrincipalKind.CONTRACTOR
    suffix = "" if name == DEFAULT_INSTANCE else f":{name}"
    return Principal(f"{kind.value}:{project_id}{suffix}", kind, project_id, name)


def parse_principal(text: str) -> Principal:
    """The principal named by an identifier the hub wrote (the inverse of `Principal.id`)."""
    if text == OPERATOR.id:
        return OPERATOR
    if text == MAIN_AGENT.id:
        return MAIN_AGENT
    parts = text.split(":")
    if len(parts) not in (2, 3) or parts[0] not in (PrincipalKind.OWNER, PrincipalKind.CONTRACTOR):
        raise ValueError("not a principal identifier")
    side = Side.OWNER if parts[0] == PrincipalKind.OWNER else Side.CONTRACTOR
    return instance_principal(side, parts[1], parts[2] if len(parts) == 3 else DEFAULT_INSTANCE)
