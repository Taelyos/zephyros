"""ATK-74 (the Z2 form): values a caller supplies for a project are plain text, and a request
with a field the system does not define is refused.

Positions P4 (the main agent) and P1. The container half (a fixed template, only the allowed
containers) runs against the container filter in Z2-06; here we check that nothing a caller
writes can reach a container specification, because only the identifier the hub issued does.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.acceptance.world import World
from zephyros.core.audit import Outcome

pytestmark = pytest.mark.acceptance

HOSTILE_NAMES = [
    "--privileged",
    "../..",
    "$(touch /tmp/pwned)",
    "`id`",
    "a; rm -rf /",
    "x --mount type=bind,source=/,target=/host",
    "${IFS}x",
    "-v /var/run/docker.sock:/var/run/docker.sock",
    "name' OR '1'='1",
]
HOSTILE_URLS = [
    "file:///etc/passwd",
    "ssh://10.0.0.5/repo",
    "http://169.254.169.254/",
    "--upload-pack=evil",
    "$(id)",
]


@pytest.mark.parametrize("creator", ["main", "operator"])
@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_atk_74_a_hostile_project_name_is_stored_as_text_and_reaches_no_specification(
    restarted: World, creator: str, name: str
) -> None:
    world = restarted
    world.step("ATK-74.name")
    result = world[creator].tool(
        "project_create",
        {"name": name, "repository_url": "https://github.com/Taelyos/zephyros"},
    )
    assert result.data is not None
    identifier = result.data["project_id"]
    assert identifier.startswith("p_")
    assert all(character in "p_0123456789abcdef" for character in identifier)
    stored = world.hub._projects.get(identifier)  # reason: the oracle looks under the hub
    assert (stored.name, stored.repository_url) == (
        name,
        "https://github.com/Taelyos/zephyros",
    )
    listed = world["main"].tool("project_list").data
    assert listed is not None
    assert name in [p["name"] for p in listed["projects"]]
    world.verify_logs()


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_atk_74_a_repository_source_the_hub_does_not_accept_is_refused(
    restarted: World, url: str
) -> None:
    """Z3, EXG-44: the URL is checked against the accepted sources — a source that is not an
    https URL of a repository hosted on GitHub is refused, logged, and nothing is stored."""
    world = restarted
    world.step("ATK-74.url")
    result = world["main"].tool("project_create", {"name": "ok", "repository_url": url})
    assert result.refused
    assert result.error is not None
    assert "repository hosted on GitHub" in json.dumps(result.error)
    world.verify_logs()


def test_atk_74_an_accepted_source_is_stored_and_reaches_no_specification(
    restarted: World,
) -> None:
    world = restarted
    url = "https://github.com/Taelyos/zephyros.git"
    result = world["main"].tool("project_create", {"name": "ok", "repository_url": url})
    assert result.data is not None
    stored = world.hub._projects.get(result.data["project_id"])  # reason: the oracle
    assert stored.repository_url == url


UNDEFINED_FIELDS: list[dict[str, Any]] = [
    {"image": "evil/image"},
    {"command": "sh -c 'id'"},
    {"entrypoint": ["sh"]},
    {"env": {"A": "b"}},
    {"environment": ["A=b"]},
    {"mount": "/"},
    {"volumes": ["/:/host"]},
    {"binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
    {"privileged": True},
    {"cap_add": ["SYS_ADMIN"]},
    {"network_mode": "host"},
    {"devices": ["/dev/sda"]},
    {"security_opt": ["seccomp=unconfined"]},
    {"user": "root"},
    {"labels": {"zephyros": "no"}},
    {"HostConfig": {"Privileged": True}},
    {"project_id": "p_1"},
    {"id": "p_x"},
]


@pytest.mark.parametrize("fields", UNDEFINED_FIELDS, ids=lambda f: next(iter(f)))
@pytest.mark.parametrize("creator", ["main", "operator"])
def test_atk_74_a_project_request_with_an_undefined_field_is_refused(
    restarted: World, creator: str, fields: dict[str, Any]
) -> None:
    world = restarted
    before = len(world["main"].tool("project_list").data["projects"])  # type: ignore[index]  # reason: data set
    result = world[creator].tool("project_create", {"name": "ok", **fields})
    assert result.refused
    assert result.error is not None
    assert result.error["error"] == "invalid"
    last = world.records("hub")[-1]
    assert last.outcome is Outcome.REFUSED
    assert len(world["main"].tool("project_list").data["projects"]) == before  # type: ignore[index]  # reason: data set


@pytest.mark.parametrize("name", ["contractor_a", "owner_a"])
def test_atk_74_an_instance_cannot_create_projects_at_all(restarted: World, name: str) -> None:
    world = restarted
    result = world[name].tool("project_create", {"name": "mine"})
    assert result.refused
    assert result.error is not None
    assert result.error["reason"] == "forbidden"


def test_atk_74_control_characters_in_a_name_are_refused_not_stored(restarted: World) -> None:
    world = restarted
    for name in ("two\nlines", "tab\there", "nul\x00", "esc\x1b[0m", "", "x" * 101):
        result = world["main"].tool("project_create", {"name": name})
        assert result.refused, repr(name)
