"""The operator and main-agent tokens: a lifetime, a rotation, a clear refusal (ADR 0026)."""

from __future__ import annotations

import json
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zephyros.adapters.http.operator import build_operator_app
from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.instance_control import FakeInstanceControl
from zephyros.adapters.sqlite.database import Database
from zephyros.app.channel_tokens import (
    SECONDS_PER_DAY,
    ChannelTokens,
    build_control_app,
    write_token_file,
)
from zephyros.app.cli import main as cli_main
from zephyros.app.cli import run_rotation
from zephyros.app.hub import HubRuntime, build_hub
from zephyros.core.audit import HUB_STREAM, Action, AuditRecord, Reason
from zephyros.core.tokens import Authenticated, Endpoint, Rejected, TokenKind

LIFETIME_DAYS = 90
DAY = SECONDS_PER_DAY


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock()
        self.database = Database(":memory:")
        self.runtime: HubRuntime = build_hub(self.database, FakeInstanceControl(), self.clock)
        self.files = {
            TokenKind.OPERATOR: tmp_path / "tokens" / "operator.token",
            TokenKind.MAIN_AGENT: tmp_path / "tokens" / "main-agent.token",
        }
        self.channel = ChannelTokens(
            self.runtime.tokens, self.runtime.audit, self.files, LIFETIME_DAYS * DAY
        )

    def secret(self, kind: TokenKind) -> str:
        return self.files[kind].read_text(encoding="ascii").strip()

    def check(self, secret: str, kind: TokenKind) -> Authenticated | Rejected:
        endpoint = Endpoint.OPERATOR if kind is TokenKind.OPERATOR else Endpoint.MAIN_AGENT
        return self.runtime.tokens.authenticate(
            secret, peer_address="172.18.0.1", endpoint=endpoint
        )

    def records(self, action: Action) -> list[dict[str, object]]:
        found: list[AuditRecord] = self.runtime.audit.read(HUB_STREAM, 0, 10_000)
        return [json.loads(r.parameters) for r in found if r.action is action]


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


# ----------------------------------------------------------------------------- the file
def test_a_token_file_is_replaced_whole_and_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "operator.token"
    write_token_file(path, "zph_h_" + "A" * 43)
    write_token_file(path, "zph_h_" + "B" * 43)
    assert path.read_text(encoding="ascii") == "zph_h_" + "B" * 43 + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["operator.token"]  # no temporary left


# ----------------------------------------------------------------------------- at start
def test_the_first_start_issues_each_token_with_the_lifetime_and_logs_it(setup: Setup) -> None:
    setup.channel.prepare()
    for kind, prefix in ((TokenKind.OPERATOR, "zph_h_"), (TokenKind.MAIN_AGENT, "zph_m_")):
        secret = setup.secret(kind)
        assert secret.startswith(prefix)
        found = setup.check(secret, kind)
        assert isinstance(found, Authenticated)
        assert found.record.expires_at == setup.clock.now() + timedelta(days=LIFETIME_DAYS)
    issued = setup.records(Action.TOKEN_ISSUE)
    assert [(r["kind"], r["why"]) for r in issued] == [
        ("operator", "no token file"),
        ("main-agent", "no token file"),
    ]
    assert all(r["expires_at"] == "2027-01-08T12:00:00.000000Z" for r in issued)


def test_a_working_token_is_kept_across_starts(setup: Setup) -> None:
    setup.channel.prepare()
    first = setup.secret(TokenKind.OPERATOR)
    setup.clock.advance(10 * DAY)
    setup.channel.prepare()
    assert setup.secret(TokenKind.OPERATOR) == first
    assert len(setup.records(Action.TOKEN_ISSUE)) == 2


def test_a_token_issued_before_the_lifetime_existed_gets_it_from_the_next_start(
    setup: Setup,
) -> None:
    """No silent break: the token keeps working for the whole lifetime, counted from the start
    that gave it one, and then it is refused."""
    old = setup.runtime.tokens.issue_operator_token()  # what an earlier version issued
    assert old.record.expires_at is None
    write_token_file(setup.files[TokenKind.OPERATOR], old.secret)
    setup.clock.advance(400 * DAY)  # long after it was issued
    setup.channel.prepare()
    assert setup.secret(TokenKind.OPERATOR) == old.secret  # kept, not replaced
    given = setup.records(Action.TOKEN_LIFETIME)
    assert [(r["kind"], r["token_id"]) for r in given] == [("operator", old.token_id)]
    setup.clock.advance(LIFETIME_DAYS * DAY - 1)
    assert isinstance(setup.check(old.secret, TokenKind.OPERATOR), Authenticated)
    setup.clock.advance(1)
    refused = setup.check(old.secret, TokenKind.OPERATOR)
    assert isinstance(refused, Rejected)
    assert refused.reason is Reason.EXPIRED


def test_a_lifetime_is_given_once_and_never_to_a_revoked_token(setup: Setup) -> None:
    revoked = setup.runtime.tokens.issue_main_agent_token()
    setup.runtime.tokens.revoke(revoked.token_id)
    setup.channel.prepare()
    setup.channel.prepare()
    assert setup.records(Action.TOKEN_LIFETIME) == []


def test_an_expired_token_in_the_file_is_replaced_at_start(setup: Setup) -> None:
    setup.channel.prepare()
    first = setup.secret(TokenKind.MAIN_AGENT)
    setup.clock.advance(LIFETIME_DAYS * DAY)
    setup.channel.prepare()
    assert setup.secret(TokenKind.MAIN_AGENT) != first
    whys = [r["why"] for r in setup.records(Action.TOKEN_ISSUE) if r["kind"] == "main-agent"]
    assert whys == ["no token file", "the token of the file is refused: expired"]


# ----------------------------------------------------------------------------- rotation
def test_a_rotation_writes_a_new_token_and_revokes_the_old_one(setup: Setup) -> None:
    setup.channel.prepare()
    old = setup.secret(TokenKind.OPERATOR)
    main_before = setup.secret(TokenKind.MAIN_AGENT)
    setup.clock.advance(DAY)
    done = setup.channel.rotate(TokenKind.OPERATOR)
    new = setup.secret(TokenKind.OPERATOR)
    assert new != old
    assert stat.S_IMODE(setup.files[TokenKind.OPERATOR].stat().st_mode) == 0o600
    assert isinstance(setup.check(new, TokenKind.OPERATOR), Authenticated)
    refused = setup.check(old, TokenKind.OPERATOR)
    assert isinstance(refused, Rejected)
    assert refused.reason is Reason.REVOKED
    assert done.expires_at == "2027-01-09T12:00:00.000000Z"
    assert len(done.revoked) == 1
    # The other kind is left alone.
    assert setup.secret(TokenKind.MAIN_AGENT) == main_before
    assert isinstance(setup.check(main_before, TokenKind.MAIN_AGENT), Authenticated)
    rotation = [
        (r["kind"], r["why"])
        for action in (Action.TOKEN_ISSUE, Action.TOKEN_REVOKE)
        for r in setup.records(action)
        if r["why"] == "rotation"
    ]
    assert rotation == [("operator", "rotation"), ("operator", "rotation")]


def test_a_rotation_revokes_every_other_live_token_of_its_kind(setup: Setup) -> None:
    """A token left by a lost file, or issued by hand, does not outlive a rotation."""
    setup.channel.prepare()
    stray = setup.runtime.tokens.issue_main_agent_token(3600)
    old = setup.secret(TokenKind.MAIN_AGENT)
    found = setup.check(old, TokenKind.MAIN_AGENT)
    assert isinstance(found, Authenticated)
    done = setup.channel.rotate(TokenKind.MAIN_AGENT)
    assert set(done.revoked) == {stray.token_id, found.record.token_id}
    for secret in (stray.secret, old):
        assert isinstance(setup.check(secret, TokenKind.MAIN_AGENT), Rejected)
    assert [r.token_id for r in setup.runtime.tokens.live_of_kind(TokenKind.MAIN_AGENT)] == [
        done.token_id
    ]


def test_a_rotation_that_cannot_write_the_file_changes_nothing(setup: Setup) -> None:
    setup.channel.prepare()
    old = setup.secret(TokenKind.OPERATOR)
    directory = setup.files[TokenKind.OPERATOR].parent
    directory.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            setup.channel.rotate(TokenKind.OPERATOR)
    finally:
        directory.chmod(0o700)
    assert setup.secret(TokenKind.OPERATOR) == old
    assert isinstance(setup.check(old, TokenKind.OPERATOR), Authenticated)
    assert len(setup.runtime.tokens.live_of_kind(TokenKind.OPERATOR)) == 1


def test_a_project_token_is_not_a_channel_token(setup: Setup) -> None:
    with pytest.raises(ValueError, match="not a token of a channel"):
        setup.channel.rotate(TokenKind.PROJECT)


# ----------------------------------------------------------------------------- the socket
def test_the_control_app_rotates_and_never_returns_the_token(setup: Setup) -> None:
    setup.channel.prepare()
    client = TestClient(build_control_app(setup.channel))
    answer = client.post("/tokens/rotate", json={"kind": "main-agent"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["ok"] is True
    assert body["file"] == str(setup.files[TokenKind.MAIN_AGENT])
    assert setup.secret(TokenKind.MAIN_AGENT) not in answer.text
    assert "zph_" not in answer.text


@pytest.mark.parametrize(
    ("payload", "status"),
    [({"kind": "project"}, 400), ({"kind": 1}, 400), (["operator"], 400), ("operator", 415)],
)
def test_the_control_app_refuses_anything_else(setup: Setup, payload: object, status: int) -> None:
    client = TestClient(build_control_app(setup.channel))
    if isinstance(payload, str):
        answer = client.post("/tokens/rotate", content=payload)
    else:
        answer = client.post("/tokens/rotate", json=payload)
    assert answer.status_code == status
    assert answer.json()["ok"] is False


def test_the_control_app_says_when_the_file_cannot_be_written(setup: Setup) -> None:
    setup.channel.prepare()
    directory = setup.files[TokenKind.OPERATOR].parent
    directory.chmod(0o500)
    try:
        answer = TestClient(build_control_app(setup.channel)).post(
            "/tokens/rotate", json={"kind": "operator"}
        )
    finally:
        directory.chmod(0o700)
    assert answer.status_code == 500
    assert "cannot be written" in answer.json()["error"]


def test_the_command_line_prints_the_answer_of_the_hub(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.channel.prepare()
    with TestClient(build_control_app(setup.channel)) as client:
        assert run_rotation(client, "operator") == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "operator"


def test_the_command_line_says_when_no_hub_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["hub", "rotate-token", "operator", "--socket", str(tmp_path / "x")]) == 1
    assert "did not answer on its control socket" in capsys.readouterr().err


# ----------------------------------------------------------------------------- the refusal
def test_an_expired_operator_token_is_told_what_to_do(setup: Setup) -> None:
    setup.channel.prepare()
    secret = setup.secret(TokenKind.OPERATOR)
    setup.clock.advance(LIFETIME_DAYS * DAY)
    client = TestClient(
        build_operator_app(setup.runtime.hub, port=8081),
        base_url="http://127.0.0.1:8081",
    )
    answer = client.post(
        "/operator/call",
        json={"tool": "whoami", "arguments": {}},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert answer.status_code == 401
    body = answer.json()
    assert body["error"] == "token_expired"
    assert "scripts/deploy.sh rotate-token operator" in body["message"]
    assert "2027-01-08T12:00:00.000000Z" in body["message"]
    refusals = [
        r
        for r in setup.runtime.audit.read(HUB_STREAM, 0, 10_000)
        if r.action is Action.AUTHENTICATION and r.reason is Reason.EXPIRED
    ]
    assert len(refusals) == 1
