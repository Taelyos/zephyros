"""The read of a container's log is bounded: the filter takes the log of a sandbox it does not trust
(ADR 0017, ATK-22), and a flood must not reach its memory."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from zephyros.adapters.docker.api import DockerApi, DockerError


def frame(stream: int, data: bytes) -> bytes:
    """One frame of the multiplexed stream a runtime gives for a container without a terminal."""
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def api_for(content: bytes | Iterator[bytes], status: int = 200) -> DockerApi:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")
    return DockerApi(client)


def test_a_log_within_the_limit_is_given_whole_from_both_streams() -> None:
    api = api_for(frame(1, b"abc") + frame(2, b"def"))
    assert api.logs_head("c", 6) == ("abcdef", False)


def test_a_log_one_byte_over_the_limit_is_cut_at_the_limit_and_says_so() -> None:
    api = api_for(frame(1, b"abcd") + frame(1, b"efg"))
    assert api.logs_head("c", 6) == ("abcdef", True)


def test_frames_may_arrive_in_pieces() -> None:
    raw = frame(1, b"first line\n") + frame(2, b"second line\n")
    pieces = iter([raw[i : i + 3] for i in range(0, len(raw), 3)])
    assert api_for(pieces).logs_head("c", 100) == ("first line\nsecond line\n", False)


def test_a_log_without_frames_is_given_as_it_is() -> None:
    """A container with a terminal has no frames."""
    assert api_for(b"plain text\n").logs_head("c", 100) == ("plain text\n", False)


def test_a_flood_is_not_read_to_its_end() -> None:
    """The stream is abandoned a little past twice the limit (a framed stream carries eight
    bytes of header per frame), so the memory of the caller does not follow the flood."""
    produced: list[int] = []

    def flood() -> Iterator[bytes]:
        for _ in range(10_000):
            produced.append(1)
            yield frame(1, b"x" * 1000)

    text, more = api_for(flood()).logs_head("c", 5000)
    assert (len(text), more) == (5000, True)
    assert len(produced) < 20


def test_the_limit_is_in_bytes_of_the_log_not_of_the_stream() -> None:
    """Many small frames carry more header than log: a log that fits is not refused for them."""
    log = b"".join(frame(1, b"ab") for _ in range(100))
    text, more = api_for(log).logs_head("c", 200)
    assert (len(text), more) == (200, False)


def test_an_error_of_the_runtime_is_a_docker_error_with_its_message() -> None:
    api = api_for(b'{"message": "no such container"}', status=404)
    with pytest.raises(DockerError, match="404: no such container"):
        api.logs_head("c", 10)


def test_a_runtime_that_cannot_be_reached_is_a_docker_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")
    with pytest.raises(DockerError, match="could not be reached"):
        DockerApi(client).logs_head("c", 10)
