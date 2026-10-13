"""The accepted repository sources (EXG-44): the annex, and only the annex."""

from __future__ import annotations

import pytest

from zephyros.core.sources import accepted

ACCEPTED = [
    "https://github.com/Taelyos/zephyros",
    "https://github.com/Taelyos/zephyros.git",
    "https://github.com/example-user/my_repo.v2",
    "https://github.com/o/r",
]

REFUSED = [
    "",
    "file:///etc/passwd",
    "file:///home/user/repo",
    "ssh://git@github.com/Taelyos/zephyros.git",
    "git@github.com:Taelyos/zephyros.git",
    "git://github.com/Taelyos/zephyros.git",
    "http://github.com/Taelyos/zephyros",
    "http://169.254.169.254/Taelyos/zephyros",
    "https://192.168.7.7/Taelyos/zephyros",
    "https://localhost/Taelyos/zephyros",
    "https://gitlab.com/Taelyos/zephyros",
    "https://github.com.evil.example/Taelyos/zephyros",
    "https://github.com@evil.example/Taelyos/zephyros",
    "https://evil.example/github.com/Taelyos/zephyros",
    "https://github.com/Taelyos/zephyros/../../etc/passwd",
    "https://github.com/Taelyos/zephyros?token=1",
    "https://github.com/Taelyos/zephyros#fragment",
    "https://user:password@github.com/Taelyos/zephyros",  # pragma: allowlist secret
    "https://github.com:8443/Taelyos/zephyros",
    "https://github.com/Taelyos",
    "https://github.com/",
    "https://github.com",
    "https://github.com/Taelyos/zephyros\n",
]


@pytest.mark.parametrize("url", ACCEPTED)
def test_the_sources_of_the_annex_are_accepted(url: str) -> None:
    assert accepted(url)


@pytest.mark.parametrize("url", REFUSED)
def test_every_other_source_is_refused(url: str) -> None:
    assert not accepted(url)
