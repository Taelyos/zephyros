"""Types of the forge interface."""

from __future__ import annotations

import pytest

from zephyros.core.ports.forge import RepositoryRef


def test_accepts_a_repository_name() -> None:
    assert RepositoryRef("organization/project").slug == "organization/project"


@pytest.mark.parametrize(
    "slug", ["", "project", "/project", "organization/", "a/b/c", "a b/c", "a/b c", "a/\tb"]
)
def test_refuses_anything_else(slug: str) -> None:
    with pytest.raises(ValueError, match="not a repository name"):
        RepositoryRef(slug)
