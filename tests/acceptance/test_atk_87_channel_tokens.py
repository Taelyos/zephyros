"""ATK-87 for the operator and main-agent tokens: they expire, are refused once expired with the
next step named, and a rotation replaces them without a restart (ADR 0026).

The world starts with the two tokens as a hub before the update issued them, without an expiry.
Oracles: O-REFUSE (the answers), O-LOG (the refusals, the issue, the revocation), and the
positive control: the tokens of a deployment that is updated keep working for the lifetime,
the rotated ones work at once, and across a restart of the hub.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.acceptance.world import World
from zephyros.app.channel_tokens import SECONDS_PER_DAY, ChannelTokens, build_control_app
from zephyros.core.audit import HUB_STREAM, Action, Outcome, Reason
from zephyros.core.tokens import TokenKind

pytestmark = pytest.mark.acceptance

LIFETIME_DAYS = 30
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def channel_of(world: World, files: dict[TokenKind, Path]) -> ChannelTokens:
    return ChannelTokens(world.tokens, world.audit, files, LIFETIME_DAYS * SECONDS_PER_DAY)


def operator_answer(world: World, token: str) -> Any:
    return world.operator_client(token=token).post(
        "/operator/call", json={"tool": "whoami", "arguments": {}}
    )


def main_answer(world: World, token: str) -> Any:
    return world.main_client(token=token).post("/mcp", json=PING)


def read(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def actions(world: World, action: Action) -> list[dict[str, Any]]:
    return [
        json.loads(record.parameters)
        for record in world.records(HUB_STREAM)
        if record.action is action
    ]


def test_atk_87_the_channel_tokens_expire_and_are_rotated(world: World, tmp_path: Path) -> None:
    files = {
        TokenKind.OPERATOR: tmp_path / "operator.token",
        TokenKind.MAIN_AGENT: tmp_path / "main-agent.token",
    }
    for kind, actor in ((TokenKind.OPERATOR, "operator"), (TokenKind.MAIN_AGENT, "main")):
        files[kind].write_text(world[actor].secret + "\n", encoding="ascii")
    operator, main = world["operator"].secret, world["main"].secret

    world.step("ATK-87.a the update gives the tokens it finds a lifetime, and breaks nothing")
    channel_of(world, files).prepare()
    assert read(files[TokenKind.OPERATOR]) == operator
    assert {r["kind"] for r in actions(world, Action.TOKEN_LIFETIME)} == {"operator", "main-agent"}
    world.clock.advance(LIFETIME_DAYS * SECONDS_PER_DAY - 1)
    assert operator_answer(world, operator).status_code == 200
    assert main_answer(world, main).status_code == 200

    world.step("ATK-87.b past the lifetime, each is refused, logged, and told what to do")
    world.clock.advance(1)
    for answer, command in (
        (operator_answer(world, operator), "rotate-token operator"),
        (main_answer(world, main), "rotate-token main-agent"),
    ):
        assert answer.status_code == 401
        assert answer.json()["error"] == "token_expired"
        assert command in answer.json()["message"]
    expired = [
        r
        for r in world.records(HUB_STREAM)
        if (r.action, r.outcome, r.reason)
        == (Action.AUTHENTICATION, Outcome.REFUSED, Reason.EXPIRED)
    ]
    assert len(expired) == 2

    world.step("ATK-87.c the rotation writes new tokens; the old ones stay refused")
    control = TestClient(build_control_app(channel_of(world, files)))
    for name in ("operator", "main-agent"):
        answer = control.post("/tokens/rotate", json={"kind": name})
        assert answer.status_code == 200, answer.text
        assert "zph_" not in answer.text
    new_operator, new_main = read(files[TokenKind.OPERATOR]), read(files[TokenKind.MAIN_AGENT])
    assert operator_answer(world, new_operator).status_code == 200
    assert main_answer(world, new_main).status_code == 200
    assert operator_answer(world, operator).status_code == 401
    assert main_answer(world, main).status_code == 401
    rotations = [r["kind"] for r in actions(world, Action.TOKEN_ISSUE) if r["why"] == "rotation"]
    assert rotations == ["operator", "main-agent"]

    world.step("ATK-87.d after a restart of the hub, the rotated tokens still work")
    world.restart_hub()
    channel_of(world, files).prepare()
    assert (read(files[TokenKind.OPERATOR]), read(files[TokenKind.MAIN_AGENT])) == (
        new_operator,
        new_main,
    )
    assert operator_answer(world, new_operator).status_code == 200
    assert main_answer(world, new_main).status_code == 200
    assert operator_answer(world, operator).status_code == 401
    world.verify_logs()
