"""Headless Chromium, driven over the DevTools protocol through a pipe.

The tests of the replay player run the real page in a real browser. Chromium speaks the DevTools
protocol on file descriptors 3 (commands in) and 4 (answers and events out) when it is started
with `--remote-debugging-pipe`: one JSON message per command, each ended by a NUL byte. So the
standard library is enough: no WebSocket client, no Node, no driver to install.

The browser is found through the `CHROMIUM` environment variable, then on the PATH, then where
macOS installs it. A machine without one skips the tests that need it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any, Self

NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
MACOS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
TIMEOUT = 30.0
FLAGS = (
    "--headless",
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--mute-audio",
    "--password-store=basic",  # Linux: no keyring prompt
    "--use-mock-keychain",  # macOS: no keychain prompt
    "--window-size=1280,720",
)


class BrowserError(Exception):
    """The browser did not start, did not answer in time, or refused a command."""


def find_chromium() -> str | None:
    """The browser to run: `CHROMIUM` when set, else the first one installed."""
    explicit = os.environ.get("CHROMIUM")
    if explicit:
        return shutil.which(explicit)
    for name in NAMES:
        found = shutil.which(name)
        if found:
            return found
    return next((path for path in MACOS if os.access(path, os.X_OK)), None)


def _above_four(fd: int) -> int:
    """The same pipe end on a descriptor above 4, so that moving it to 3 or 4 clobbers nothing.

    The copy keeps the close-on-exec flag: only the copies on 3 and 4 reach the browser.
    """
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


class Chromium:
    """One headless browser and one page, until `close`.

    Every uncaught exception of the page and every error it logs to the console is kept in
    `errors`, in the order it came.
    """

    def __init__(self, executable: str) -> None:
        self._profile = tempfile.mkdtemp(prefix="zephyros-chromium-")
        commands_in, commands_out = (_above_four(fd) for fd in os.pipe())
        answers_in, answers_out = (_above_four(fd) for fd in os.pipe())
        self._commands, self._answers = commands_out, answers_in
        # The browser's own messages go to a file: a pipe nobody reads could fill and stall it.
        self._log = Path(self._profile) / "stderr.log"
        flags = [*FLAGS, f"--user-data-dir={self._profile}"]
        if os.geteuid() == 0:
            flags.append("--no-sandbox")  # a browser refuses to run sandboxed as root
        self._pid = os.posix_spawn(
            executable,
            [executable, *flags],
            dict(os.environ),
            file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_OPEN, 2, str(self._log), os.O_WRONLY | os.O_CREAT, 0o600),
                (os.POSIX_SPAWN_DUP2, commands_in, 3),
                (os.POSIX_SPAWN_DUP2, answers_out, 4),
            ],
        )
        self._running = True
        os.close(commands_in)
        os.close(answers_out)
        self._buffer = b""
        self._next = 0
        self._session = ""
        self._events: list[dict[str, Any]] = []
        self.errors: list[str] = []
        try:
            target = self.command("Target.createTarget", {"url": "about:blank"})["targetId"]
            attached = self.command("Target.attachToTarget", {"targetId": target, "flatten": True})
            self._session = str(attached["sessionId"])
            for domain in ("Page", "Runtime", "Log"):
                self.command(f"{domain}.enable")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ the protocol
    def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message).encode("utf-8") + b"\0"
        while data:
            data = data[os.write(self._commands, data) :]

    def _receive(self, deadline: float) -> dict[str, Any]:
        while b"\0" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError("the browser did not answer in time")
            ready, _, _ = select.select([self._answers], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self._answers, 1 << 16)
            if not chunk:
                raise BrowserError(f"the browser closed the pipe: {self._stderr()}")
            self._buffer += chunk
        message, _, self._buffer = self._buffer.partition(b"\0")
        received: dict[str, Any] = json.loads(message)
        return received

    def _keep(self, message: dict[str, Any]) -> None:
        """An event: kept for `wait_for`, and noted when it reports an error of the page."""
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            thrown = details.get("exception") or {}
            self.errors.append(
                f"exception: {thrown.get('description') or details.get('text', '?')}"
            )
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            texts = [str(arg.get("value", arg.get("description", ""))) for arg in params["args"]]
            self.errors.append("console: " + " ".join(texts))
        elif method == "Log.entryAdded" and params.get("entry", {}).get("level") == "error":
            self.errors.append("log: " + str(params["entry"].get("text", "")))
        self._events.append(message)

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command to the page (or to the browser before a page is attached)."""
        self._next += 1
        identifier = self._next
        message: dict[str, Any] = {"id": identifier, "method": method, "params": params or {}}
        if self._session:
            message["sessionId"] = self._session
        self._send(message)
        deadline = time.monotonic() + TIMEOUT
        while True:
            answer = self._receive(deadline)
            if answer.get("id") != identifier:
                self._keep(answer)
                continue
            if "error" in answer:
                raise BrowserError(f"{method}: {answer['error']}")
            result: dict[str, Any] = answer.get("result") or {}
            return result

    def wait_for(self, method: str, timeout: float = TIMEOUT) -> dict[str, Any]:
        """The next event named `method`, received since the last one taken."""
        deadline = time.monotonic() + timeout
        while True:
            for index, event in enumerate(self._events):
                if event.get("method") == method:
                    del self._events[: index + 1]
                    params: dict[str, Any] = event.get("params") or {}
                    return params
            self._events.clear()
            self._keep(self._receive(deadline))

    # ------------------------------------------------------------------ the page
    def open(self, page: Path, query: str = "") -> None:
        """Load a local page and wait for its load event."""
        self._events.clear()
        url = page.resolve().as_uri() + (f"?{query}" if query else "")
        self.command("Page.navigate", {"url": url})
        self.wait_for("Page.loadEventFired")

    def evaluate(self, expression: str) -> Any:
        """The value of a JavaScript expression in the page; a thrown error fails the call."""
        result = self.command(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            thrown = details.get("exception") or {}
            raise BrowserError(
                f"{expression}: {thrown.get('description') or details.get('text', '?')}"
            )
        return result.get("result", {}).get("value")

    def poll(self, expression: str, timeout: float = TIMEOUT) -> Any:
        """Evaluate `expression` until it is truthy; its value, or a timeout."""
        deadline = time.monotonic() + timeout
        while True:
            value = self.evaluate(expression)
            if value:
                return value
            if time.monotonic() > deadline:
                raise BrowserError(f"still false after {timeout} s: {expression}")
            time.sleep(0.05)

    # ------------------------------------------------------------------ the end
    def _stopped(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the browser to stop; whether it did."""
        deadline = time.monotonic() + timeout
        while self._running:
            try:
                pid, _ = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                pid = self._pid
            if pid:
                self._running = False
            elif time.monotonic() > deadline:
                return False
            else:
                time.sleep(0.05)
        return True

    def _stderr(self) -> str:
        """The end of what the browser said, once it has stopped."""
        if not self._stopped(5):
            return "(the browser still runs)"
        return self._log.read_text(encoding="utf-8", errors="replace")[-2000:]

    def close(self) -> None:
        try:
            if self._running:
                self._session = ""
                with contextlib.suppress(BrowserError, OSError):
                    self.command("Browser.close")
                if not self._stopped(10):
                    os.kill(self._pid, signal.SIGKILL)
                    self._stopped(10)
        finally:
            for fd in (self._commands, self._answers):
                with contextlib.suppress(OSError):
                    os.close(fd)
            shutil.rmtree(self._profile, ignore_errors=True)
