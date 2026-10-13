"""The notifier of the hub: one signed delivery per event, and a redirect is never a
destination (EXG-43, EXG-48, ATK-83)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from zephyros.adapters.webhooks import Posted
from zephyros.app.hub import build_egress, build_notifier
from zephyros.core.config import Config

URL = "http://notify.example/hook"
SECRET = "a-shared-secret-of-16-plus"  # pragma: allowlist secret


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
