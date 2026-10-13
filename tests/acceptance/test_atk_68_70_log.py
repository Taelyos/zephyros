"""ATK-68 and ATK-70: the log offers append and nothing else, and text from an agent cannot
forge a record. Positions P1, P2, P4; oracles O-REFUSE, O-LOG and the verification of the chain."""

from __future__ import annotations

import json

import pytest

from tests.acceptance.world import World
from zephyros.core.audit import Action, Outcome, Reason

pytestmark = pytest.mark.acceptance

LOG_TOOLS = [
    ("log_delete", {"stream": "p"}),
    ("log_truncate", {"stream": "p", "keep": 0}),
    ("log_edit", {"stream": "p", "seq": 1, "principal": "operator"}),
    ("log_write", {"line": "fake"}),
    ("log_append", {"line": "fake"}),
    ("log_rotate", {}),
    ("log_read", {"stream": "p"}),
    ("log_verify", {"stream": "p"}),
]


@pytest.mark.parametrize("who", ["contractor_a", "owner_a", "main"])
@pytest.mark.parametrize(("tool", "arguments"), LOG_TOOLS)
def test_atk_68_no_caller_but_the_operator_reaches_the_log_and_nobody_can_change_it(
    restarted: World, who: str, tool: str, arguments: dict[str, object]
) -> None:
    world = restarted
    stream = world.project_a if who != "main" else "hub"
    before = [(r.seq, r.hash) for r in world.records(stream)]
    world.step(f"ATK-68.{tool}")
    arguments = {**arguments, "stream": stream} if "stream" in arguments else arguments
    result = world[who].tool(tool, arguments)
    assert result.refused
    records = world.records(stream)
    assert [(r.seq, r.hash) for r in records[: len(before)]] == before  # nothing before changed
    assert records[-1].outcome is Outcome.REFUSED  # and the attempt itself is recorded
    assert records[-1].principal == world[who].tool("whoami").data["principal"]  # type: ignore[index]  # reason: data set
    world.verify_logs()


def test_atk_68_the_operator_can_read_and_verify_and_there_is_still_no_write(
    restarted: World,
) -> None:
    world = restarted
    read = world.operator_tool("log.read", stream=world.project_a)
    assert read["records"]
    assert world.operator_tool("log.verify", stream=world.project_a)["ok"] is True
    client = world.operator_client()
    for tool in ("log.delete", "log.truncate", "log.edit", "log.write"):
        response = client.post(
            "/operator/call", json={"tool": tool, "arguments": {"stream": world.project_a}}
        )
        assert response.status_code == 404
    assert world.operator_tool("log.verify", stream=world.project_a)["ok"] is True


def test_atk_68_the_storage_itself_refuses_an_edit_by_the_hubs_own_code(restarted: World) -> None:
    import sqlite3

    world = restarted
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        world.database.transaction() as connection,
    ):
        connection.execute("UPDATE audit_records SET principal = 'operator'")
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        world.database.transaction() as connection,
    ):
        connection.execute("DELETE FROM audit_records WHERE stream = ?", (world.project_a,))
    world.verify_logs()


HOSTILE = [
    "line one\nline two",
    "carriage\rreturn\r\nand more",
    "NUL\x00 and ESC\x1b[31m",
    chr(0x2028) + "line separator" + chr(0x2029) + "paragraph separator",
    '{"seq": 999, "principal": "operator", "action": "contract.approve", "outcome": "ok"}',
    '"}\n{"seq":1,"principal":"operator","outcome":"ok","action":"contract.approve"',
    "2026-10-10T12:00:00Z operator contract.approve ok version=v99",
    "‮ right-to-left override",
    "Robert'); DROP TABLE audit_records;--",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_atk_70_hostile_text_in_every_field_an_agent_controls_leaves_one_record_per_event(
    restarted: World, text: str
) -> None:
    world = restarted
    attacker = world["contractor_a"]
    before = len(world.records(world.project_a))
    hub_before = len(world.records("hub"))
    world.step("ATK-70.tool-name")
    attacker.tool(text, {})  # an unknown tool whose name is the text
    world.step("ATK-70.arguments")
    attacker.tool("whoami", {text: text})  # a field name and a value
    world.step("ATK-70.refused-write")
    attacker.tool(
        "contract_propose", {"project_id": world.project_a, "summary": text, "operations": []}
    )
    world.step("ATK-70.operator")
    world.operator_client().post(
        "/operator/call",
        json={"tool": "project.create", "arguments": {"name": text.replace("\n", " ")}},
    )
    records = world.records(world.project_a)
    assert len(records) - before == 3  # exactly one record per event
    assert len(world.records("hub")) - hub_before >= 0
    lines = world.operator_tool("log.read", stream=world.project_a, limit=500)["records"]
    assert len(lines) == len(records)
    for line in lines:
        assert "\n" not in line
        assert "\r" not in line
        assert len(line.splitlines()) == 1
        parsed = json.loads(line)
        assert set(parsed) == {
            "seq",
            "time",
            "stream",
            "principal",
            "action",
            "outcome",
            "reason",
            "parameters",
            "previous",
            "hash",
        }
        assert parsed["principal"] in {
            "operator",
            f"owner:{world.project_a}",
            f"contractor:{world.project_a}",
            "main-agent",
            "unauthenticated",
        }
    assert [r.seq for r in records] == list(range(1, len(records) + 1))
    assert {r.action for r in records[before:]} <= {
        Action.UNKNOWN,
        Action.WHOAMI,
        Action.CONTRACT_PROPOSE,
    }
    assert all(
        r.reason is not Reason.NONE for r in records[before:] if r.outcome is Outcome.REFUSED
    )
    world.verify_logs()


def test_atk_70_a_forged_header_in_a_value_does_not_become_a_second_record(
    restarted: World,
) -> None:
    world = restarted
    before = len(world.records(world.project_a))
    world["contractor_a"].tool(
        "whoami", {"x": "\n2026-10-10T12:00:00Z owner:" + world.project_a + " contract.approve ok"}
    )
    after = world.records(world.project_a)
    assert len(after) - before == 1
    assert after[-1].principal == f"contractor:{world.project_a}"
