"""ATK-12, ATK-85 and ATK-87: identity comes from the server-side record and the connection.

Positions P1 and P2 (instances), P3 (an instance of another project), P4 (the main agent), P5
(a host on the LAN). Oracles: O-REFUSE, O-LOG, and the common positive control: each context
calls with its own token, and a restarted hub still knows them.
"""

from __future__ import annotations

import base64
import json

import pytest

from tests.acceptance.world import World
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.tokens import Endpoint

pytestmark = pytest.mark.acceptance


def jwt(header: dict[str, str], claims: dict[str, str], signature: str = "") -> str:
    def part(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part(header)}.{part(claims)}.{signature}"


def refused_with(world: World, stream: str, reason: Reason) -> None:
    last = world.records(stream)[-1]
    assert (last.action, last.outcome, last.reason) == (
        Action.AUTHENTICATION,
        Outcome.REFUSED,
        reason,
    )


# ----------------------------------------------------------------------------- ATK-12
def test_atk_12_forged_identities_are_refused(restarted: World) -> None:
    world = restarted
    contractor = world["contractor_a"]
    forgeries = {
        "alg=none": jwt({"alg": "none"}, {"side": "owner", "project": world.project_a}),
        "edited claims": jwt(
            {"alg": "HS256"}, {"side": "owner", "project": world.project_a}, "AAAA"
        ),
        "unsigned owner token": "zph_o_" + "A" * 43,
        "a token that looks right": "zph_c_" + "B" * 43,
        "plain text": f"owner:{world.project_a}",
        "empty": "",
    }
    for label, token in forgeries.items():
        world.step(f"ATK-12.forged {label}")
        response = contractor.http(token=token).post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 401, label
    world.step("ATK-12.headers")
    response = contractor.http().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "contract_propose", "arguments": {"project_id": world.project_a}},
        },
        headers={
            "X-Zephyros-Side": "owner",
            "X-Zephyros-Project": world.project_a,
            "X-Role": "owner",
        },
    )
    body = response.json()["result"]
    assert body["isError"] is True  # still a Contractor: the headers changed nothing
    world.step("ATK-12.body-fields")
    assert contractor.tool("whoami", {"side": "owner", "project": world.project_a}).refused
    assert contractor.tool("whoami").data is not None
    assert contractor.tool("whoami").data["side"] == "contractor"  # type: ignore[index]  # reason: data set
    world.verify_logs()


def test_atk_12_a_token_replayed_after_its_instance_stopped_is_refused(restarted: World) -> None:
    world = restarted
    contractor = world["contractor_a"]
    world.step("ATK-12.before-stop")
    assert contractor.tool("whoami").data is not None
    world.tokens.revoke_instance("i_contractor_a")
    world.step("ATK-12.replay")
    assert contractor.rpc("tools/list").status_code == 401
    refused_with(world, world.project_a, Reason.REVOKED)


def test_atk_12_the_owner_token_found_in_a_file_or_a_tool_output(restarted: World) -> None:
    """At Z2 the Owner token can be found by the Contractor; presenting it is ATK-85."""
    world = restarted
    stolen = world["owner_a"].secret
    assert world["contractor_a"].rpc("tools/list", token=stolen).status_code == 401
    refused_with(world, world.project_a, Reason.WRONG_SOURCE)


# ----------------------------------------------------------------------------- ATK-85
def test_atk_85_a_token_works_only_from_the_context_it_was_issued_to(restarted: World) -> None:
    world = restarted
    positions = {
        "the Owner's token from the Contractor": (
            world["owner_a"].secret,
            world["contractor_a"].address,
        ),
        "the Contractor's token from the Owner": (
            world["contractor_a"].secret,
            world["owner_a"].address,
        ),
        "project A's token from an instance of project B": (
            world["owner_a"].secret,
            world["owner_b"].address,
        ),
        "project A's token from the other side of project B": (
            world["contractor_a"].secret,
            world["contractor_b"].address,
        ),
        "a valid token from a host on the LAN": (world["owner_a"].secret, "192.168.7.50"),
        "a valid token from the host loopback": (world["owner_a"].secret, "127.0.0.1"),
        "a valid token from the hub's own address": (world["owner_a"].secret, "172.18.0.1"),
    }
    for label, (token, address) in positions.items():
        world.step(f"ATK-85.{label}")
        response = (
            world["contractor_a"]
            .http(address=address, token=token)
            .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        )
        assert response.status_code == 401, label
    last = world.records(world.project_a)[-1]
    assert last.reason is Reason.WRONG_SOURCE
    world.verify_logs()


def test_atk_85_a_forwarded_source_or_a_claimed_context_changes_nothing(restarted: World) -> None:
    world = restarted
    owner_address = world["owner_a"].address
    client = world["contractor_a"].http(token=world["owner_a"].secret)
    client.headers.update(
        {
            "X-Forwarded-For": owner_address,
            "X-Real-IP": owner_address,
            "Forwarded": f"for={owner_address}",
            "X-Zephyros-Side": "owner",
            "X-Zephyros-Project": world.project_a,
        }
    )
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert (
        json.loads(world.records(world.project_a)[-1].parameters)["source"]
        == world["contractor_a"].address
    )


def test_atk_85_the_main_agent_token_from_every_execution_context(restarted: World) -> None:
    world = restarted
    main = world["main"].secret
    for name in ("owner_a", "contractor_a", "owner_b", "contractor_b"):
        world.step(f"ATK-85.main-agent token from {name}")
        response = (
            world[name]
            .http(token=main)
            .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        )
        assert response.status_code == 401
    last = world.records("hub")[-1]
    assert (last.action, last.reason) == (Action.AUTHENTICATION, Reason.WRONG_ENDPOINT)
    # and the main-agent endpoint refuses every project token
    for name in ("owner_a", "contractor_a"):
        client = world.main_client(token=world[name].secret, address=world[name].address)
        assert (
            client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code
            == 401
        )


def test_atk_85_positive_control_each_context_calls_with_its_own_token(restarted: World) -> None:
    world = restarted
    for name in ("owner_a", "contractor_a", "owner_b", "contractor_b"):
        world.step(f"ATK-85.positive {name}")
        data = world[name].tool("whoami").data
        assert data is not None
        assert data["principal"].endswith(
            world.project_a if name.endswith("_a") else world.project_b
        )
    # a restarted instance calls with its new token
    old = world["contractor_a"]
    world.tokens.revoke_instance("i_contractor_a")
    from zephyros.core.ports.agent_engine import Side
    from zephyros.core.principals import instance_principal

    renewed = world.tokens.issue_project_token(
        instance_principal(Side.CONTRACTOR, world.project_a), "i_contractor_a", old.address, 3600
    )
    assert (
        old.http(token=renewed.secret)
        .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        .status_code
        == 200
    )
    assert old.rpc("ping").status_code == 401  # the old token is gone with the old container


# ----------------------------------------------------------------------------- ATK-87
def test_atk_87_a_token_that_is_not_rotated_expires_on_the_hubs_clock(tmp_path) -> None:  # type: ignore[no-untyped-def]  # reason: pytest fixture
    world = World(tmp_path, token_lifetime=300)
    owner = world["owner_a"]
    world.step("ATK-87.before-expiry")
    world.clock.advance(299)
    assert owner.rpc("ping").status_code == 200
    world.step("ATK-87.at-expiry")
    world.clock.advance(1)
    assert owner.rpc("ping").status_code == 401
    assert world.records(world.project_a)[-1].reason is Reason.EXPIRED
    world.restart_hub()  # a restart of the hub does not give the token back its life
    assert owner.rpc("ping").status_code == 401


def test_atk_87_a_token_expires_and_a_copy_gives_nothing_after_the_rotation(tmp_path) -> None:  # type: ignore[no-untyped-def]  # reason: pytest fixture
    world = World(tmp_path, token_lifetime=600)
    contractor = world["contractor_a"]
    from zephyros.core.ports.agent_engine import Side
    from zephyros.core.principals import instance_principal

    principal = instance_principal(Side.CONTRACTOR, world.project_a)
    copy = contractor.secret
    current = contractor.secret
    world.step("ATK-87.keep-calling")
    for _lifetime in range(4):
        for _tick in range(20):
            world.clock.advance(30)
            assert (
                contractor.http(token=current)
                .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                .status_code
                == 200
            )
            live = world.tokens.live_project_token("i_contractor_a")
            assert live is not None
            if world.tokens.needs_rotation(live, 120):
                issued = world.tokens.issue_project_token(
                    principal, "i_contractor_a", contractor.address, 600
                )
                assert (
                    contractor.http(token=issued.secret)
                    .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                    .status_code
                    == 200
                )
                world.tokens.revoke(live.token_id)
                current = issued.secret
        world.restart_hub()  # the hub restarting does not interrupt the instance either
    world.step("ATK-87.stolen-copy")
    assert (
        contractor.http(token=copy)
        .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        .status_code
        == 401
    )
    reasons = {
        r.reason for r in world.records(world.project_a) if r.action is Action.AUTHENTICATION
    }
    assert reasons & {Reason.REVOKED, Reason.EXPIRED}
    world.step("ATK-87.time-from-the-hub")
    # The caller cannot move time: no field of a request reaches the clock.
    assert (
        contractor.http(token=current)
        .post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Date": "Mon, 01 Jan 2001 00:00:00 GMT"},
        )
        .status_code
        == 200
    )
    world.verify_logs()
    assert Endpoint.AGENT.value == "agent"
