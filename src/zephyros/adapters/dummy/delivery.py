"""A delivery area in memory: trees in a dictionary, packages in a small JSON format.

A package is `{"format": "zephyros-tree", "base": <base id or null>, "files": {path: base64}}`.
It stands in for the git implementation in tests; it checks what the interface promises.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping

from zephyros.core.ports.delivery import (
    BRANCH,
    INTERFACE_VERSION,
    MAX_PACKAGE,
    Delivery,
    DeliveryError,
    Tree,
)

FORMAT = "zephyros-tree"


def make_package(files: Mapping[str, bytes], base: str | None = None) -> bytes:
    """A package of `files`, descending from `base` (None for a first state)."""
    encoded = {path: base64.b64encode(content).decode("ascii") for path, content in files.items()}
    return json.dumps({"format": FORMAT, "base": base, "files": encoded}, sort_keys=True).encode()


def state_id(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode() + b"\0" + hashlib.sha256(files[path]).digest())
    return digest.hexdigest()[:40]


def _read(package: bytes) -> tuple[str | None, dict[str, bytes]]:
    if len(package) > MAX_PACKAGE:
        raise DeliveryError("the package is too large")
    try:
        data = json.loads(package)
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise DeliveryError("not a package of this area")
        files = {str(p): base64.b64decode(c, validate=True) for p, c in data["files"].items()}
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
        raise DeliveryError("the package cannot be read") from None
    base = data.get("base")
    return (None if base is None else str(base)), files


class InMemoryDeliveryArea:
    def __init__(self) -> None:
        self._bases: dict[str, dict[str, bytes]] = {}
        self._base_ids: dict[str, str] = {}
        self._deliveries: dict[str, dict[str, tuple[Delivery, dict[str, bytes]]]] = {}

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    def prepare(self, project_id: str, package: bytes, branch: str) -> str:
        if not branch.strip():
            raise DeliveryError("the branch name is empty")
        _base, files = _read(package)
        self._bases[project_id] = files
        self._base_ids[project_id] = state_id(files)
        self._deliveries.setdefault(project_id, {})
        return self._base_ids[project_id]

    def base_package(self, project_id: str) -> bytes:
        return make_package(self._base(project_id))

    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        base_files = self._base(project_id)
        if not BRANCH.fullmatch(branch):
            raise DeliveryError("the branch name is not plain")
        kept = self._deliveries[project_id]
        if any(d.branch == branch for d, _ in kept.values()):
            raise DeliveryError("this branch was already delivered")
        base, files = _read(package)
        if base != self._base_ids[project_id]:
            raise DeliveryError("the delivery does not descend from the base")
        changed = tuple(
            sorted(p for p in set(files) | set(base_files) if files.get(p) != base_files.get(p))
        )
        delivery = Delivery(
            project_id,
            f"d{len(kept) + 1}",
            branch,
            self._base_ids[project_id],
            state_id(files),
            changed,
        )
        kept[delivery.delivery_id] = (delivery, files)
        return delivery

    def get(self, project_id: str, delivery_id: str) -> Delivery:
        return self._kept(project_id, delivery_id)[0]

    def tree(self, project_id: str, ref: str) -> Tree:
        if ref == "base":
            return Tree(self._base(project_id))
        return Tree(self._kept(project_id, ref)[1])

    def package(self, project_id: str, delivery_id: str) -> bytes:
        delivery, files = self._kept(project_id, delivery_id)
        return make_package(files, delivery.base)

    def _base(self, project_id: str) -> dict[str, bytes]:
        if project_id not in self._bases:
            raise DeliveryError("the project has no staging area")
        return self._bases[project_id]

    def _kept(self, project_id: str, delivery_id: str) -> tuple[Delivery, dict[str, bytes]]:
        found = self._deliveries.get(project_id, {}).get(delivery_id)
        if found is None:
            raise DeliveryError("there is no such delivery")
        return found
