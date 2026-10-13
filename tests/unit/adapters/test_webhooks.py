"""The sender, against a witness receiver, over a real socket (EXG-43, ATK-78)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from zephyros.adapters.http.server import build_server, stop_all
from zephyros.adapters.webhooks import WebhookSender
from zephyros.core.webhooks import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
    verify,
)

SECRET = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
BODY = b'{"event":"delivery.decided","delivery":"d_0001"}'


class Witness:
    """A receiver that verifies every delivery, refuses the false ones and logs the refusals.
    Only an accepted delivery would run a turn (ATK-78: "shall run no agent turn")."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.refusals: list[str] = []
        self.turns = 0

    async def hook(self, request: Request) -> JSONResponse:
        raw = request.headers.get(TIMESTAMP_HEADER, "")
        timestamp = int(raw) if raw.isdigit() else None
        reason = verify(
            SECRET,
            delivery_id=request.headers.get(DELIVERY_HEADER),
            timestamp=timestamp,
            body=await request.body(),
            signature=request.headers.get(SIGNATURE_HEADER),
            seen=self.seen,
            now=int(time.time()),
        )
        if reason is not None:
            self.refusals.append(reason)
            return JSONResponse({"refused": reason}, status_code=401)
        delivery_id = request.headers[DELIVERY_HEADER]
        self.seen.add(delivery_id)
        self.turns += 1
        return JSONResponse({"ok": True})


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def witness() -> Iterator[tuple[Witness, str]]:
    witness = Witness()
    app = Starlette(routes=[Route("/hook", witness.hook, methods=["POST"])])
    port = free_port()
    server = build_server(app, "127.0.0.1", port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "the witness did not start"
        time.sleep(0.02)
    try:
        yield witness, f"http://127.0.0.1:{port}/hook"
    finally:
        stop_all([server])
        thread.join(timeout=15)


def test_an_honest_delivery_is_accepted_and_verifies(witness: tuple[Witness, str]) -> None:
    receiver, url = witness
    sender = WebhookSender(url, SECRET)
    status = sender.deliver("w_0001", int(time.time()), BODY).status
    assert status == 200
    assert receiver.turns == 1
    assert receiver.refusals == []


def test_the_attacks_of_atk_78_are_refused_and_run_no_turn(witness: tuple[Witness, str]) -> None:
    receiver, url = witness
    sender = WebhookSender(url, SECRET)
    now = int(time.time())
    statuses = []
    # No signature, a wrong signature, a modified body: three posts the sender never signs.
    statuses.append(httpx.post(url, content=BODY).status_code)
    statuses.append(
        httpx.post(
            url,
            content=BODY,
            headers={
                DELIVERY_HEADER: "w_1",
                TIMESTAMP_HEADER: str(now),
                SIGNATURE_HEADER: "0" * 64,
            },
        ).status_code
    )
    statuses.append(
        httpx.post(
            url,
            content=BODY + b" ",
            headers={
                DELIVERY_HEADER: "w_2",
                TIMESTAMP_HEADER: str(now),
                SIGNATURE_HEADER: sign(SECRET, "w_2", now, BODY),
            },
        ).status_code
    )
    # A valid old signature and a valid signature over a stale timestamp.
    old = now - 3600
    statuses.append(
        httpx.post(
            url,
            content=BODY,
            headers={
                DELIVERY_HEADER: "w_3",
                TIMESTAMP_HEADER: str(old),
                SIGNATURE_HEADER: sign(SECRET, "w_3", old, BODY),
            },
        ).status_code
    )
    statuses.append(
        httpx.post(
            url,
            content=BODY,
            headers={
                DELIVERY_HEADER: "w_4",
                TIMESTAMP_HEADER: str(old),
                SIGNATURE_HEADER: sign(SECRET, "w_4", old, BODY),
            },
        ).status_code
    )
    # A repeated delivery: the first one is honest, the second repeats its identifier.
    assert sender.deliver("w_5", now, BODY).status == 200
    statuses.append(sender.deliver("w_5", now, BODY).status)
    assert statuses == [401, 401, 401, 401, 401, 401]
    assert receiver.turns == 1  # the honest one, and no turn for any refusal
    assert receiver.refusals == [
        "missing",
        "bad_signature",
        "bad_signature",
        "stale_timestamp",
        "stale_timestamp",
        "replayed",
    ]
