"""A small client of the Docker Engine API.

It speaks HTTP over a Unix socket and exposes the handful of calls the filter needs. It has no
policy: which calls to make, on which objects, with which specifications, is decided by
`zephyros.adapters.filter.executor` and the templates of `zephyros.core.containers`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

API_VERSION = "v1.43"
TIMEOUT_SECONDS = 60.0


class DockerError(Exception):
    """The runtime answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"{status}: {message}")


class DockerApi:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    @classmethod
    def over_socket(cls, path: str) -> DockerApi:
        transport = httpx.HTTPTransport(uds=path)
        return cls(
            httpx.Client(transport=transport, base_url="http://docker", timeout=TIMEOUT_SECONDS)
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ plumbing
    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, str] | None = None,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method, f"/{API_VERSION}{path}", json=json_body, params=params
            )
        except httpx.HTTPError as error:
            raise DockerError(
                0, f"the runtime could not be reached ({type(error).__name__})"
            ) from None
        if response.status_code >= 400 and response.status_code not in allow:
            raise _failure(response)
        return response

    # ------------------------------------------------------------------ networks
    def create_network(self, spec: dict[str, Any]) -> None:
        self._call("POST", "/networks/create", json_body=spec)

    def inspect_network(self, name: str) -> dict[str, Any] | None:
        response = self._call("GET", f"/networks/{name}", allow=(404,))
        return None if response.status_code == 404 else dict(response.json())

    def remove_network(self, name: str) -> None:
        self._call("DELETE", f"/networks/{name}")

    def connect(self, network: str, container: str, address: str | None = None) -> None:
        body: dict[str, Any] = {"Container": container}
        if address is not None:
            body["EndpointConfig"] = {"IPAMConfig": {"IPv4Address": address}}
        self._call("POST", f"/networks/{network}/connect", json_body=body)

    def disconnect(self, network: str, container: str) -> None:
        self._call(
            "POST",
            f"/networks/{network}/disconnect",
            json_body={"Container": container, "Force": False},
        )

    # ------------------------------------------------------------------ volumes
    def create_volume(self, spec: dict[str, Any]) -> None:
        self._call("POST", "/volumes/create", json_body=spec)

    def inspect_volume(self, name: str) -> dict[str, Any] | None:
        response = self._call("GET", f"/volumes/{name}", allow=(404,))
        return None if response.status_code == 404 else dict(response.json())

    def remove_volume(self, name: str) -> None:
        self._call("DELETE", f"/volumes/{name}")

    # ------------------------------------------------------------------ containers
    def create_container(self, name: str, spec: dict[str, Any]) -> None:
        self._call("POST", "/containers/create", json_body=spec, params={"name": name})

    def start(self, name: str) -> None:
        self._call("POST", f"/containers/{name}/start", allow=(304,))

    def stop(self, name: str, seconds: int) -> None:
        self._call("POST", f"/containers/{name}/stop", params={"t": str(seconds)}, allow=(304,))

    def remove(self, name: str) -> None:
        self._call("DELETE", f"/containers/{name}", params={"force": "true", "v": "false"})

    def kill(self, name: str) -> None:
        self._call("POST", f"/containers/{name}/kill", allow=(404, 409))

    def put_archive(self, name: str, path: str, archive: bytes) -> None:
        """Extract a tar archive at `path` in the container (it may be stopped)."""
        try:
            response = self._client.put(
                f"/{API_VERSION}/containers/{name}/archive",
                params={"path": path, "noOverwriteDirNonDir": "true"},
                content=archive,
                headers={"Content-Type": "application/x-tar"},
            )
        except httpx.HTTPError as error:
            raise DockerError(
                0, f"the runtime could not be reached ({type(error).__name__})"
            ) from None
        if response.status_code >= 400:
            raise DockerError(response.status_code, response.text[:300])

    def wait(self, name: str, timeout: float) -> int | None:
        """The exit code of the container, or None when it is still running after `timeout`."""
        try:
            response = self._client.post(f"/{API_VERSION}/containers/{name}/wait", timeout=timeout)
        except httpx.TimeoutException:
            return None
        except httpx.HTTPError as error:
            raise DockerError(
                0, f"the runtime could not be reached ({type(error).__name__})"
            ) from None
        if response.status_code >= 400:
            raise DockerError(response.status_code, response.text[:300])
        return int(response.json().get("StatusCode", -1))

    def logs_head(self, name: str, limit: int) -> tuple[str, bool]:
        """What the container wrote, both streams, from its beginning, and whether it wrote more
        than `limit` bytes of it.

        The stream is read as it comes and abandoned a little past twice the limit (a framed
        stream carries eight bytes of header per frame), so that what a container floods its log
        with does not reach the memory of the caller. The text is cut to `limit` bytes."""
        raw = bytearray()
        try:
            with self._client.stream(
                "GET",
                f"/{API_VERSION}/containers/{name}/logs",
                params={"stdout": "1", "stderr": "1"},
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _failure(response)
                for chunk in response.iter_bytes():
                    raw += chunk
                    if len(raw) > 2 * limit + 8:
                        break
        except httpx.HTTPError as error:
            raise DockerError(
                0, f"the runtime could not be reached ({type(error).__name__})"
            ) from None
        log = demultiplex(bytes(raw))
        return log[:limit].decode("utf-8", errors="replace"), len(log) > limit

    def inspect(self, name: str) -> dict[str, Any] | None:
        response = self._call("GET", f"/containers/{name}/json", allow=(404,))
        return None if response.status_code == 404 else dict(response.json())

    def list_containers(self, label: str) -> list[dict[str, Any]]:
        response = self._call(
            "GET",
            "/containers/json",
            params={"all": "1", "filters": json.dumps({"label": [label]})},
        )
        return [dict(item) for item in response.json()]

    def events(self, since: int, until: int, label: str) -> list[dict[str, Any]]:
        response = self._call(
            "GET",
            "/events",
            params={
                "since": str(since),
                "until": str(until),
                "filters": json.dumps({"type": ["container"], "label": [label]}),
            },
        )
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _failure(response: httpx.Response) -> DockerError:
    try:
        message = str(response.json().get("message", response.text))
    except ValueError:
        message = response.text
    return DockerError(response.status_code, message[:300])


def demultiplex(raw: bytes) -> bytes:
    """The bytes of a log stream without the 8-byte frame headers the runtime adds when the
    container has no terminal. Anything that is not framed is returned as it is."""
    out = bytearray()
    position = 0
    while (
        position + 8 <= len(raw)
        and raw[position] in (0, 1, 2)
        and raw[position + 1 : position + 4] == b"\0\0\0"
    ):
        size = int.from_bytes(raw[position + 4 : position + 8], "big")
        out += raw[position + 8 : position + 8 + size]
        position += 8 + size
    if position == 0:
        return raw
    return bytes(out) + raw[position:]
