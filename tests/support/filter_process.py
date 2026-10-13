"""The container filter, running for real in a thread on a Unix socket, over a fake runtime."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from tests.support.fake_docker import FakeDocker
from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.app.filter import run
from zephyros.core.config import Config


class Running:
    """The filter, serving in a thread, until `close`."""

    def __init__(self, config: Config, docker: FakeDocker, journal: MemoryJournal) -> None:
        self.config = config
        self._loop = asyncio.new_event_loop()
        self._stop = asyncio.Event()
        self._failure: list[BaseException] = []
        self._thread = threading.Thread(
            target=self._serve, args=(docker, journal), daemon=True, name="filter"
        )
        self._thread.start()
        self.socket = Path(config.paths.filter_socket)
        deadline = time.monotonic() + 10
        while not self.socket.is_socket() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.socket.is_socket(), self._failure

    def _serve(self, docker: FakeDocker, journal: MemoryJournal) -> None:
        try:
            self._loop.run_until_complete(
                run(self.config, docker=docker.api(), journal=journal, stop=self._stop)
            )
        except BaseException as error:  # reason: reported by the test that started it
            self._failure.append(error)

    def client(self) -> FilterClient:
        return FilterClient.over_socket(str(self.socket))

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=10)
        assert not self._thread.is_alive()
        assert not self._failure, self._failure
