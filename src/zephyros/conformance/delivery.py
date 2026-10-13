"""Conformance suite of the delivery interface.

To check an implementation, subclass `DeliveryConformance` and implement `make_fixture`: an
area, and a function that makes a package of given files descending from a given package (None
for a first state), in the implementation's format.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from zephyros.conformance._support import ConformanceError, check, check_interface_version
from zephyros.core.ports.delivery import INTERFACE_VERSION, DeliveryArea, DeliveryError

PROJECT = "p_conformance"
BASE = {"README.md": b"# A project\n", "calc.py": b"def add(a, b):\n    return a + b\n"}
CHANGED = {
    **BASE,
    "calc.py": b"def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
}
ADDED = {**BASE, "tests/test_new.py": b"def test_new():\n    pass\n"}


@dataclass(frozen=True)
class DeliveryFixture:
    area: DeliveryArea
    derive: Callable[[Mapping[str, bytes], bytes | None], bytes]
    """`derive(files, parent)`: a package of `files` descending from the package `parent`."""


class DeliveryConformance:
    """Rules every delivery area follows."""

    def make_fixture(self) -> DeliveryFixture:
        raise NotImplementedError

    def _prepared(self) -> tuple[DeliveryFixture, bytes]:
        fixture = self.make_fixture()
        fixture.area.prepare(PROJECT, fixture.derive(BASE, None), "main")
        return fixture, fixture.area.base_package(PROJECT)

    @staticmethod
    def _must_refuse(action: Callable[[], object], what: str) -> None:
        try:
            action()
        except DeliveryError:
            return
        raise ConformanceError(f"{what} was accepted")

    def test_follows_the_tested_major_version(self) -> None:
        check_interface_version(self.make_fixture().area.interface_version, INTERFACE_VERSION)

    def test_keeps_the_base_as_it_was_given(self) -> None:
        fixture, _base = self._prepared()
        check(dict(fixture.area.tree(PROJECT, "base").files) == BASE, "the base tree differs")

    def test_keeps_a_delivery_with_its_changed_paths_and_its_tree(self) -> None:
        fixture, base = self._prepared()
        delivery = fixture.area.receive(PROJECT, fixture.derive(CHANGED, base), "add-sub")
        check(delivery.branch == "add-sub", "the delivery lost its branch name")
        check(delivery.changed_paths == ("calc.py",), f"changed paths: {delivery.changed_paths}")
        check(delivery.base != delivery.head, "the delivery has the identifier of its base")
        check(fixture.area.get(PROJECT, delivery.delivery_id) == delivery, "read back differs")
        tree = fixture.area.tree(PROJECT, delivery.delivery_id)
        check(dict(tree.files) == CHANGED, "the tree of the delivery differs")
        check(fixture.area.package(PROJECT, delivery.delivery_id), "the package is empty")

    def test_gives_each_delivery_its_own_identifier(self) -> None:
        fixture, base = self._prepared()
        first = fixture.area.receive(PROJECT, fixture.derive(CHANGED, base), "one")
        second = fixture.area.receive(PROJECT, fixture.derive(ADDED, base), "two")
        check(first.delivery_id != second.delivery_id, "two deliveries share an identifier")
        check(second.changed_paths == ("tests/test_new.py",), "an added file is not a change")

    def test_refuses_a_branch_name_that_is_not_plain(self) -> None:
        fixture, base = self._prepared()
        for name in ("", "../x", "Main", "a b", "x" * 70, "-x", "a/b", "a\nb"):
            self._must_refuse(
                lambda name=name: fixture.area.receive(  # type: ignore[misc]  # reason: bound default
                    PROJECT, fixture.derive(CHANGED, base), name
                ),
                f"the branch name {name!r}",
            )

    def test_refuses_a_branch_delivered_twice(self) -> None:
        fixture, base = self._prepared()
        fixture.area.receive(PROJECT, fixture.derive(CHANGED, base), "once")
        self._must_refuse(
            lambda: fixture.area.receive(PROJECT, fixture.derive(ADDED, base), "once"),
            "a branch delivered twice",
        )

    def test_refuses_what_does_not_descend_from_the_base(self) -> None:
        fixture, _base = self._prepared()
        stranger = fixture.derive({"other.txt": b"x"}, None)
        self._must_refuse(
            lambda: fixture.area.receive(PROJECT, fixture.derive(CHANGED, stranger), "stranger"),
            "a delivery that does not descend from the base",
        )
        self._must_refuse(
            lambda: fixture.area.receive(PROJECT, b"not a package", "garbage"), "garbage"
        )

    def test_refuses_an_unknown_project_or_delivery(self) -> None:
        fixture, base = self._prepared()
        self._must_refuse(
            lambda: fixture.area.receive("p_nobody", fixture.derive(CHANGED, base), "x"),
            "a delivery to an unknown project",
        )
        self._must_refuse(lambda: fixture.area.get(PROJECT, "d999"), "an unknown delivery")
        self._must_refuse(lambda: fixture.area.tree(PROJECT, "d999"), "the tree of nothing")
