"""Tokens (EXG-42, EXG-57, EXG-60; ATK-12, ATK-85, ATK-87 on the hub side)."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.token_store import InMemoryTokenStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.token_store import SqliteTokenStore
from zephyros.core.audit import Reason
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import OPERATOR, instance_principal
from zephyros.core.tokens import (
    Authenticated,
    Endpoint,
    Rejected,
    TokenKind,
    TokenRecord,
    TokenService,
    TokenStore,
    hash_secret,
    normalize_address,
)

PROJECT = "p_abcdef12"
OTHER_PROJECT = "p_zzzzzz99"
OWNER = instance_principal(Side.OWNER, PROJECT)
CONTRACTOR = instance_principal(Side.CONTRACTOR, PROJECT)
OWNER_ADDRESS = "10.200.0.2"
CONTRACTOR_ADDRESS = "10.200.0.18"
LIFETIME = 3600


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[TokenStore]:
    if request.param == "memory":
        yield InMemoryTokenStore()
    else:
        database = Database(":memory:")
        yield SqliteTokenStore(database)
        database.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(store: TokenStore, clock: FakeClock) -> TokenService:
    return TokenService(store, clock)


def project_call(
    service: TokenService, secret: str, address: str | None, endpoint: Endpoint = Endpoint.AGENT
) -> Authenticated | Rejected:
    return service.authenticate(secret, peer_address=address, endpoint=endpoint)


def reason(result: Authenticated | Rejected) -> Reason | None:
    return result.reason if isinstance(result, Rejected) else None


# ----------------------------------------------------------------------------- issue
def test_a_project_token_has_a_kind_prefix_and_256_bits(
    service: TokenService, store: TokenStore
) -> None:
    owner = service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    contractor = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, LIFETIME)
    assert re.fullmatch(r"zph_o_[A-Za-z0-9_-]{43}", owner.secret)
    assert re.fullmatch(r"zph_c_[A-Za-z0-9_-]{43}", contractor.secret)
    assert owner.secret != contractor.secret
    assert service.issue_main_agent_token().secret.startswith("zph_m_")
    assert service.issue_operator_token().secret.startswith("zph_h_")


def test_only_the_hash_is_stored_and_the_secret_is_never_printed(
    service: TokenService, store: TokenStore
) -> None:
    issued = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, LIFETIME)
    record = store.get(issued.token_id)
    assert record is not None
    assert record.token_hash == hash_secret(issued.secret)
    assert issued.secret not in repr(record)
    assert issued.secret not in repr(issued)
    assert issued.secret not in json.dumps(record.token_hash)


def test_issuing_refuses_nonsense(service: TokenService) -> None:
    with pytest.raises(ValueError, match="belongs to an instance"):
        service.issue_project_token(OPERATOR, "i", OWNER_ADDRESS, LIFETIME)
    with pytest.raises(ValueError, match="IP address"):
        service.issue_project_token(OWNER, "i", "not-an-address", LIFETIME)
    with pytest.raises(ValueError, match="positive lifetime"):
        service.issue_project_token(OWNER, "i", OWNER_ADDRESS, 0)


def test_a_broken_secret_generator_is_refused(store: TokenStore, clock: FakeClock) -> None:
    service = TokenService(store, clock, new_secret=lambda: "short")
    with pytest.raises(ValueError, match="malformed"):
        service.issue_project_token(OWNER, "i", OWNER_ADDRESS, LIFETIME)


# ----------------------------------------------------------------------------- identity
def test_the_right_context_is_attributed_to_its_side_and_project(service: TokenService) -> None:
    owner = service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    contractor = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, LIFETIME)
    owner_result = project_call(service, owner.secret, OWNER_ADDRESS)
    contractor_result = project_call(service, contractor.secret, CONTRACTOR_ADDRESS)
    assert isinstance(owner_result, Authenticated)
    assert owner_result.principal == OWNER
    assert owner_result.principal.project_id == PROJECT
    assert isinstance(contractor_result, Authenticated)
    assert contractor_result.principal == CONTRACTOR
    assert contractor_result.principal.side is Side.CONTRACTOR


def test_a_token_presented_from_another_context_is_refused(service: TokenService) -> None:
    owner = service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    other = instance_principal(Side.OWNER, OTHER_PROJECT)
    foreign = service.issue_project_token(other, "i_foreign", "10.200.0.34", LIFETIME)
    # The Contractor presents the Owner's token (found in a file or a tool output).
    assert reason(project_call(service, owner.secret, CONTRACTOR_ADDRESS)) is Reason.WRONG_SOURCE
    # An instance of another project presents a token of project A.
    assert reason(project_call(service, owner.secret, "10.200.0.34")) is Reason.WRONG_SOURCE
    # A host on the LAN presents a valid token.
    assert reason(project_call(service, owner.secret, "192.168.1.50")) is Reason.WRONG_SOURCE
    assert reason(project_call(service, foreign.secret, OWNER_ADDRESS)) is Reason.WRONG_SOURCE


@pytest.mark.parametrize(
    "peer", [None, "", "unix", "10.200.0.2.5", "10.200.0.3", "::1", "garbage\n10.200.0.2"]
)
def test_a_peer_address_that_is_missing_or_different_is_refused(
    service: TokenService, peer: str | None
) -> None:
    issued = service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    assert reason(project_call(service, issued.secret, peer)) is Reason.WRONG_SOURCE


def test_an_ipv4_peer_written_as_ipv6_is_the_same_peer(service: TokenService) -> None:
    issued = service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    assert isinstance(project_call(service, issued.secret, "::ffff:10.200.0.2"), Authenticated)
    assert isinstance(project_call(service, issued.secret, "[::ffff:10.200.0.2]"), Authenticated)
    assert normalize_address("::ffff:10.200.0.2") == "10.200.0.2"
    assert normalize_address(None) is None
    assert normalize_address("x") is None


def test_each_kind_of_token_is_accepted_on_its_own_endpoint_only(service: TokenService) -> None:
    project = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, LIFETIME)
    main = service.issue_main_agent_token()
    operator = service.issue_operator_token()
    # The main-agent token and the operator token presented by an execution context.
    assert reason(project_call(service, main.secret, CONTRACTOR_ADDRESS)) is Reason.WRONG_ENDPOINT
    assert (
        reason(project_call(service, operator.secret, CONTRACTOR_ADDRESS)) is Reason.WRONG_ENDPOINT
    )
    assert (
        reason(project_call(service, project.secret, CONTRACTOR_ADDRESS, Endpoint.MAIN_AGENT))
        is Reason.WRONG_ENDPOINT
    )
    assert (
        reason(project_call(service, project.secret, CONTRACTOR_ADDRESS, Endpoint.OPERATOR))
        is Reason.WRONG_ENDPOINT
    )
    assert (
        reason(project_call(service, main.secret, None, Endpoint.OPERATOR)) is Reason.WRONG_ENDPOINT
    )
    assert isinstance(
        project_call(service, main.secret, "172.18.0.1", Endpoint.MAIN_AGENT), Authenticated
    )
    assert isinstance(
        project_call(service, operator.secret, "172.18.0.1", Endpoint.OPERATOR), Authenticated
    )


# ----------------------------------------------------------------------------- ATK-12
def unsigned_jwt(header: dict[str, str], claims: dict[str, str]) -> str:
    """A JWT with `alg=none` and no signature, built at run time."""

    def part(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part(header)}.{part(claims)}."


FORGERIES = [
    "",
    "Bearer",
    unsigned_jwt({"alg": "none"}, {"side": "owner", "project": PROJECT}),
    unsigned_jwt({"alg": "none", "typ": "JWT"}, {"project": "a", "side": "owner"}),
    "zph_o_" + "A" * 43,
    "zph_c_" + "A" * 42,
    "zph_c_" + "A" * 44,
    "zph_x_" + "A" * 43,
    "ZPH_C_" + "A" * 43,
    '{"side":"owner","project":"p_abcdef12"}',
    "owner:" + PROJECT,
    "zph_c_" + "A" * 43 + "\n",
]


@pytest.mark.parametrize("forged", FORGERIES)
def test_a_forged_or_unissued_token_is_unknown(service: TokenService, forged: str) -> None:
    service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    result = project_call(service, forged, OWNER_ADDRESS)
    assert reason(result) is Reason.UNKNOWN_TOKEN
    assert isinstance(result, Rejected)
    assert result.token_id is None
    assert forged not in repr(result) or forged == ""


@settings(max_examples=200, deadline=None)
@given(text=st.text(max_size=120))
def test_no_text_authenticates_by_chance(text: str) -> None:
    service = TokenService(InMemoryTokenStore(), FakeClock())
    service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    assert isinstance(project_call(service, text, OWNER_ADDRESS), Rejected)


def test_a_missing_secret_is_refused(service: TokenService) -> None:
    assert (
        reason(service.authenticate(None, peer_address=OWNER_ADDRESS, endpoint=Endpoint.AGENT))
        is Reason.UNKNOWN_TOKEN
    )


def test_a_rejection_names_the_kind_presented_and_never_the_token(service: TokenService) -> None:
    issued = service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    result = project_call(service, issued.secret, CONTRACTOR_ADDRESS)
    assert isinstance(result, Rejected)
    assert result.presented == "zph_o_"
    assert result.token_id == issued.token_id
    assert issued.secret not in repr(result)


# ----------------------------------------------------------------------------- revocation
def test_a_token_stops_working_when_its_instance_stops(service: TokenService) -> None:
    first = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, LIFETIME)
    second = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, LIFETIME)
    bystander = service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    assert service.revoke_instance("i_con") == 2
    assert reason(project_call(service, first.secret, CONTRACTOR_ADDRESS)) is Reason.REVOKED
    assert reason(project_call(service, second.secret, CONTRACTOR_ADDRESS)) is Reason.REVOKED
    assert isinstance(project_call(service, bystander.secret, OWNER_ADDRESS), Authenticated)
    assert service.revoke_instance("i_con") == 0
    assert not service.revoke(first.token_id)
    assert not service.revoke("t_unknown")


# ----------------------------------------------------------------------------- ATK-87
def test_a_token_expires_at_the_end_of_its_lifetime_on_the_hub_clock(
    service: TokenService, clock: FakeClock
) -> None:
    issued = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, 600)
    clock.advance(599)
    assert isinstance(project_call(service, issued.secret, CONTRACTOR_ADDRESS), Authenticated)
    clock.advance(1)
    assert reason(project_call(service, issued.secret, CONTRACTOR_ADDRESS)) is Reason.EXPIRED


def test_a_running_instance_keeps_calling_across_several_lifetimes_by_rotation(
    service: TokenService, clock: FakeClock
) -> None:
    lifetime, rotate_before = 600, 120
    current = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, lifetime)
    stolen_copies = [current.secret]
    for _ in range(4):  # four lifetimes
        for _ in range(20):  # every 30 s the instance calls
            clock.advance(30)
            assert isinstance(
                project_call(service, current.secret, CONTRACTOR_ADDRESS), Authenticated
            )
            live = service.live_project_token("i_con")
            assert live is not None
            if service.needs_rotation(live, rotate_before):
                # The new token is valid before the old one is revoked.
                new = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, lifetime)
                assert isinstance(
                    project_call(service, new.secret, CONTRACTOR_ADDRESS), Authenticated
                )
                assert isinstance(
                    project_call(service, current.secret, CONTRACTOR_ADDRESS), Authenticated
                )
                service.revoke(live.token_id)
                current = new
                stolen_copies.append(new.secret)
    # The first token was copied; after the rotation and the lifetime it gives nothing.
    assert reason(project_call(service, stolen_copies[0], CONTRACTOR_ADDRESS)) in (
        Reason.REVOKED,
        Reason.EXPIRED,
    )
    assert isinstance(project_call(service, current.secret, CONTRACTOR_ADDRESS), Authenticated)
    assert len(stolen_copies) >= 5


def test_needs_rotation_and_the_live_token(service: TokenService, clock: FakeClock) -> None:
    issued = service.issue_project_token(CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, 600)
    live = service.live_project_token("i_con")
    assert live is not None
    assert live.token_id == issued.token_id
    assert not service.needs_rotation(live, 120)
    clock.advance(480)
    assert service.needs_rotation(live, 120)
    clock.advance(120)
    assert service.live_project_token("i_con") is None
    assert not service.needs_rotation(service.issue_operator_token().record, 120)


def test_operator_tokens_may_have_a_lifetime(service: TokenService, clock: FakeClock) -> None:
    issued = service.issue_main_agent_token(lifetime_seconds=60)
    assert isinstance(
        service.authenticate(
            issued.secret, peer_address="172.18.0.1", endpoint=Endpoint.MAIN_AGENT
        ),
        Authenticated,
    )
    clock.advance(60)
    result = service.authenticate(
        issued.secret, peer_address="172.18.0.1", endpoint=Endpoint.MAIN_AGENT
    )
    assert reason(result) is Reason.EXPIRED


# ----------------------------------------------------------------------------- records
def test_a_record_enforces_its_shape() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError, match="bound to an address"):
        TokenRecord("t_1", "h", TokenKind.PROJECT, OWNER, None, None, clock.now(), None)
    with pytest.raises(ValueError, match="only a project token"):
        TokenRecord("t_1", "h", TokenKind.OPERATOR, OPERATOR, "i", "10.0.0.1", clock.now(), None)


def test_a_store_refuses_a_duplicate(store: TokenStore, clock: FakeClock) -> None:
    service = TokenService(store, clock, new_secret=lambda: "A" * 43)
    service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)
    with pytest.raises(Exception):  # noqa: B017, PT011  # reason: each store raises its own error type
        service.issue_project_token(OWNER, "i_owner", OWNER_ADDRESS, LIFETIME)


def test_tokens_survive_a_restart_of_the_sqlite_store(tmp_path) -> None:  # type: ignore[no-untyped-def]  # reason: pytest fixture
    path = tmp_path / "hub.db"
    clock = FakeClock()
    database = Database(path)
    issued = TokenService(SqliteTokenStore(database), clock).issue_project_token(
        CONTRACTOR, "i_con", CONTRACTOR_ADDRESS, LIFETIME
    )
    database.close()
    reopened = Database(path)
    service = TokenService(SqliteTokenStore(reopened), clock)
    result = project_call(service, issued.secret, CONTRACTOR_ADDRESS)
    assert isinstance(result, Authenticated)
    assert result.principal == CONTRACTOR
    reopened.close()
