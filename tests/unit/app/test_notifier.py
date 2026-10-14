"""The notifier of the hub: one signed delivery per event, and a redirect is never a
destination (EXG-43, EXG-48, ATK-83). The hub program sends them from a thread of their own: a
slow receiver holds up no listener (T03)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from tests.support.sockets import socket_path
from tests.unit.core.contract_fixtures import change, put, requirement
from zephyros.adapters.memory.instance_control import FakeInstanceControl
from zephyros.adapters.sqlite.contract_store import SqliteContractStore
from zephyros.adapters.sqlite.database import Database
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.adapters.sqlite.project_store import SqliteProjectStore
from zephyros.adapters.system_clock import SystemClock
from zephyros.adapters.webhooks import Posted
from zephyros.app.hub import build_egress, build_notifier, run
from zephyros.core.clock import format_time
from zephyros.core.config import Config, parse_config
from zephyros.core.contract import ContractService
from zephyros.core.deliveries import DeliveryRecord, DeliveryState
from zephyros.core.principals import OPERATOR
from zephyros.core.projects import ProjectService
from zephyros.core.webhooks import (
    DELIVERY_HEADER,
    EVENTS_SCHEMA_VERSION,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)

URL = "http://notify.example/hook"
SECRET = "a-shared-secret-of-16-plus"  # pragma: allowlist secret
DIGEST = "ghcr.io/example/app@sha256:" + "e" * 64
SLOW_SECONDS = 5.0


def config_for(url: str = URL) -> Config:
    return cast(
        "Config",
        SimpleNamespace(
            notifications=SimpleNamespace(url=url, secret=SECRET),
            gateway=SimpleNamespace(upstream_url=None),
        ),
    )


def test_the_configured_hosts_hold_the_receiver_and_the_sources() -> None:
    policy = build_egress(config_for())
    assert policy.hosts() == ["github.com", "notify.example"]


def test_no_configuration_means_no_notifier() -> None:
    assert build_notifier(config_for("")) is None


def test_an_accepted_answer_is_a_delivery_made() -> None:
    posted: list[Posted] = []

    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Posted:
        posted.append(Posted(url, headers, body, 200))
        return posted[-1]

    notify = build_notifier(config_for(), post=post)
    assert notify is not None
    result = notify("delivery.decided", {"project_id": "p_1", "delivery_id": "d_1"})
    assert (result.status, result.refused) == (200, "")
    assert posted[0].url == URL
    assert b'"event": "delivery.decided"' in posted[0].body


def test_a_redirect_to_an_unlisted_host_is_refused_never_followed() -> None:
    posted: list[Posted] = []

    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Posted:
        posted.append(Posted(url, headers, body, 302, "http://elsewhere.example:8091/steal"))
        return posted[-1]

    notify = build_notifier(config_for(), post=post)
    assert notify is not None
    result = notify("continuity.alert_main", {"project_id": "p_1", "side": "contractor"})
    assert result.refused == "elsewhere.example"
    assert result.status == 302
    assert [p.url for p in posted] == [URL]  # the redirect target was never contacted


def test_a_redirect_to_a_configured_host_is_still_not_followed() -> None:
    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Posted:
        return Posted(url, headers, body, 307, "https://github.com/elsewhere")

    notify = build_notifier(config_for(), post=post)
    assert notify is not None
    result = notify("delivery.loop", {"project_id": "p_1"})
    assert (result.status, result.refused) == (307, "")  # not followed; not an egress refusal


# ----------------------------------------------------------------------------- T03, end to end
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class SlowReceiver:
    """The main agent's endpoint, slow to answer: it holds each request up to five seconds,
    or until the test lets it go."""

    def __init__(self) -> None:
        self.received = threading.Event()
        self.answered = threading.Event()
        self.release = threading.Event()
        self.requests: list[tuple[dict[str, str], bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                receiver.requests.append((dict(self.headers.items()), body))
                receiver.received.set()
                receiver.release.wait(SLOW_SECONDS)
                self.send_response(204)
                self.end_headers()
                receiver.answered.set()

            def log_message(self, *_arguments: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/hook"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def receiver() -> Iterator[SlowReceiver]:
    slow = SlowReceiver()
    try:
        yield slow
    finally:
        slow.close()


def hub_config(tmp_path: Path, url: str) -> Config:
    return parse_config(
        {
            "schema_version": "1.0.0",
            "images": {"instance": DIGEST, "guard": DIGEST},
            "hub": {
                "agent_port": free_port(),
                "operator_port": free_port(),
                "main_agent_port": free_port(),
            },
            "notifications": {"url": url, "secret": SECRET},
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "filter_socket": str(tmp_path / "filter.sock"),
                "operator_token_file": str(tmp_path / "tokens" / "operator.token"),
                "main_agent_token_file": str(tmp_path / "tokens" / "main-agent.token"),
                "control_socket": socket_path(tmp_path),
            },
        }
    )


def a_decided_delivery(data_dir: Path) -> tuple[str, str]:
    """What an earlier run of the hub left in its database: a project, its contract and a
    delivery the gate decided."""
    data_dir.mkdir(parents=True)
    database = Database(data_dir / "hub.db")
    clock = SystemClock()
    project = ProjectService(SqliteProjectStore(database), clock).create(
        OPERATOR, {"name": "Served"}
    )
    ContractService(SqliteContractStore(database), clock).apply_reserved(
        OPERATOR, project.id, change(put(requirement(1)))
    )
    SqliteDeliveryStore(database).put_record(
        DeliveryRecord(
            project_id=project.id,
            delivery_id="d1",
            branch="feature",
            state=DeliveryState.DECIDED,
            received_at=format_time(clock.now()),
            decision="PASS",
            contract_version=1,
        )
    )
    database.close()
    return project.id, "d1"


class RunningHub:
    """The hub program on its three listeners, in a thread, as `zephyros-hub` runs it."""

    def __init__(self, config: Config) -> None:
        self._loop = asyncio.new_event_loop()
        self._stop = asyncio.Event()
        self._thread = threading.Thread(
            target=lambda: self._loop.run_until_complete(
                run(
                    config, agent_bind="127.0.0.1", stop=self._stop, instances=FakeInstanceControl()
                )
            ),
            daemon=True,
        )
        self._thread.start()
        self.operator = f"http://127.0.0.1:{config.hub.operator_port}"
        self.agent = f"http://127.0.0.1:{config.hub.agent_port}"
        wait_until(self._healthy, "the hub did not start")
        self._token = Path(config.paths.operator_token_file).read_text(encoding="ascii").strip()

    def _healthy(self) -> bool:
        try:
            return httpx.get(f"{self.operator}/operator/health", timeout=1).status_code == 200
        except httpx.TransportError:
            return False

    def call(self, tool: str, timeout: float = 10, **arguments: Any) -> dict[str, Any]:
        answer = httpx.post(
            f"{self.operator}/operator/call",
            json={"tool": tool, "arguments": arguments},
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=timeout,
        )
        body: dict[str, Any] = answer.json()
        return body

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=30)
        assert not self._thread.is_alive()


def wait_until(check: Callable[[], bool], message: str, seconds: float = 15) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, message
        time.sleep(0.05)


def test_a_review_reaches_the_main_agent_signed_and_a_slow_receiver_holds_up_nothing(
    tmp_path: Path, receiver: SlowReceiver
) -> None:
    """T03 (I1, I5): `delivery.reviewed` is documented as notified. The receiver takes five
    seconds to answer; meanwhile the hub answers the review and serves its agent endpoint."""
    config = hub_config(tmp_path, receiver.url)
    project, delivery = a_decided_delivery(Path(config.paths.data_dir))
    hub = RunningHub(config)
    try:
        answers: list[dict[str, Any]] = []
        review = threading.Thread(
            target=lambda: answers.append(
                hub.call(
                    "delivery.review",
                    timeout=30,
                    project_id=project,
                    delivery_id=delivery,
                    verdict="reject",
                    reasons="The subtraction is missing.",
                )
            ),
            daemon=True,
        )
        review.start()
        assert receiver.received.wait(10), "no notification of the review reached the receiver"
        # the receiver holds the notification; the hub does not wait for it
        listing = httpx.get(f"{hub.agent}/v1/models", timeout=2)
        assert listing.status_code == 200
        review.join(timeout=2)
        assert answers, "the review waited for the receiver"
        assert answers[0]["ok"] is True, answers[0]
        assert not receiver.answered.is_set()  # all of it within the receiver's five seconds

        ((headers, body),) = receiver.requests
        assert (
            verify(
                SECRET,
                delivery_id=headers.get(DELIVERY_HEADER),
                timestamp=int(headers[TIMESTAMP_HEADER]),
                body=body,
                signature=headers.get(SIGNATURE_HEADER),
                seen=set(),
                now=int(time.time()),
            )
            is None
        )
        event = json.loads(body)
        assert event["event"] == "delivery.reviewed"
        assert (event["project_id"], event["delivery_id"]) == (project, delivery)
        assert (event["verdict"], event["decision"]) == ("reject", "PASS")
        assert event["simulation"] is False
        assert event["schema_version"] == EVENTS_SCHEMA_VERSION

        receiver.release.set()

        def sent() -> bool:
            records = [
                json.loads(line) for line in hub.call("log.read", stream=project)["data"]["records"]
            ]
            return any(
                record["action"] == "notification.sent" and record["outcome"] == "ok"
                for record in records
            )

        wait_until(sent, "the notification was not recorded once answered")
    finally:
        receiver.release.set()
        hub.close()
