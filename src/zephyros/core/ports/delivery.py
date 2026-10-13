"""The delivery interface: where the deliveries of a project are kept, and how a tree is read.

A delivery is the work the Contractor pushes into the staging area of the hub and declares to
it. The core speaks of deliveries, bases and trees, never of how they are stored. The default
implementation keeps each delivery as a git branch in a bare repository of the hub
(`zephyros.adapters.git.delivery`); a package is then a git bundle.

Version 0.x.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

INTERFACE_VERSION = "0.1.0"
BRANCH = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
"""The name of a delivered branch: short, plain, and the same on every platform."""
MAX_PACKAGE = 32 * 1024 * 1024


class DeliveryError(Exception):
    """The delivery area refused the request or could not honor it."""


@dataclass(frozen=True, slots=True)
class Delivery:
    project_id: str
    delivery_id: str
    branch: str
    base: str
    """The identifier of the base the delivery starts from."""
    head: str
    """The identifier of the delivered state."""
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Tree:
    """The files of one state: relative paths to contents. Read-only once built."""

    files: Mapping[str, bytes]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


class DeliveryArea(Protocol):
    @property
    def interface_version(self) -> str: ...

    def prepare(self, project_id: str, package: bytes, branch: str) -> str:
        """Make `branch` of the package the base of the project. Returns the base identifier.
        Preparing again replaces the base; deliveries already kept stay."""
        ...

    def base_package(self, project_id: str) -> bytes:
        """The base, as a package an instance can start from."""
        ...

    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        """Keep a pushed branch as a new delivery. Refused: an unknown project, a branch name
        outside `BRANCH`, a package too large or unreadable, a state that does not descend from
        the base, and a branch already delivered."""
        ...

    def get(self, project_id: str, delivery_id: str) -> Delivery: ...

    def tree(self, project_id: str, ref: str) -> Tree:
        """The files of the base (`ref` = "base") or of a delivery (its identifier)."""
        ...

    def package(self, project_id: str, delivery_id: str) -> bytes:
        """A delivery as a package, to push it to the forge."""
        ...
