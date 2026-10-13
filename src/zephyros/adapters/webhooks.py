"""Sending the signed deliveries (EXG-43), hub side.

The transport is injectable: the deployment posts over httpx, the tests hand a recorder. The
sender signs exactly what `webhooks.verify` checks on the other side.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from zephyros.core.webhooks import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)


@dataclass(frozen=True, slots=True)
class Posted:
    url: str
    headers: dict[str, str]
    body: bytes
    status: int
    location: str = ""
    """Where the answer points, when it is a redirect; the hub follows none (EXG-48)."""


Post = Callable[[str, dict[str, str], bytes, float], Posted]
"""Post the body with the headers to the url within the timeout; return the answer."""


def _post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Posted:
    response = httpx.post(url, content=body, headers=headers, timeout=timeout)
    return Posted(url, headers, body, response.status_code, response.headers.get("location", ""))


class WebhookSender:
    """One destination: where the deliveries go, and the secret that signs them."""

    def __init__(
        self, url: str, secret: str, *, post: Post | None = None, timeout: float = 10.0
    ) -> None:
        self._url = url
        self._secret = secret
        self._post = post or _post
        self._timeout = timeout

    def deliver(self, delivery_id: str, timestamp: int, body: bytes) -> Posted:
        """Send one signed delivery; return the answer the destination gave."""
        headers = {
            DELIVERY_HEADER: delivery_id,
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign(self._secret, delivery_id, timestamp, body),
            "Content-Type": "application/json",
        }
        return self._post(self._url, headers, body, self._timeout)
