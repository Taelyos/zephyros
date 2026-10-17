"""The tests kit's plugin, unit-tested without a container (ATK-22, ADR 0017).

The plugin keeps delivered code out of the judging process: an import barrier that refuses every
/work module but the asked test files, and a non-dumpable process. These are the two mechanisms
the forgery defence rests on, so they are checked here directly; the end-to-end behaviour is in
tests/runtime/test_sandbox_runner_conforms.py.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import marshal
import os
import subprocess
import sys
import textwrap
import types
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

KIT_FILE = Path(__file__).parents[2] / "images" / "kit-tests" / "zephyros_kit.py"


@pytest.fixture
def kit() -> Iterator[ModuleType]:
    """The kit plugin, loaded from the image as a standalone module. Importing it only dups
    stdout and invents a nonce; it installs nothing (that happens in a pytest hook)."""
    spec = importlib.util.spec_from_file_location("zephyros_kit_undertest", KIT_FILE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        module.REAL_OUT.close()


def _delivery_tree(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / "test_owner.py").write_text("def test_owner():\n    assert True\n", encoding="utf-8")
    (work / "payload.py").write_text("SECRET = 1\n", encoding="utf-8")
    return work


def _barrier(kit: ModuleType, work: Path, allowed: set[Path]) -> Any:
    """The barrier as the kit builds it: each asked file as it is listed, and where it really is."""
    listed = {str(p.absolute()): str(p.resolve()) for p in allowed}
    return kit._WorkImportBarrier(listed, str(work.resolve()))


def test_a_delivered_module_cannot_be_imported(kit: ModuleType, tmp_path: Path) -> None:
    work = _delivery_tree(tmp_path)
    barrier = _barrier(kit, work, allowed={work / "test_owner.py"})
    sys.path.insert(0, str(work))
    try:
        with pytest.raises(ImportError, match="black box"):
            barrier.find_spec("payload")
    finally:
        sys.path.remove(str(work))


def test_an_asked_test_file_is_allowed(kit: ModuleType, tmp_path: Path) -> None:
    """The asked test files are the only /work modules the judge may import: the barrier lets
    them through (returns None, so the real finder loads them)."""
    work = _delivery_tree(tmp_path)
    barrier = _barrier(kit, work, allowed={work / "test_owner.py"})
    sys.path.insert(0, str(work))
    try:
        assert barrier.find_spec("test_owner") is None
    finally:
        sys.path.remove(str(work))


def test_a_link_out_of_the_tree_does_not_escape_the_barrier(
    kit: ModuleType, tmp_path: Path
) -> None:
    """A subprocess of a test can make a link in the tree to a file it wrote elsewhere, to be
    imported later by a test that still imports the delivery. The module is judged by where it is
    listed as well as by where it really is, so the link is a delivered module like any other."""
    work = _delivery_tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "linked.py").write_text("SECRET = 2\n", encoding="utf-8")
    (work / "linked.py").symlink_to(elsewhere / "linked.py")
    barrier = _barrier(kit, work, allowed={work / "test_owner.py"})
    sys.path.insert(0, str(work))
    try:
        with pytest.raises(ImportError, match="black box"):
            barrier.find_spec("linked")
    finally:
        sys.path.remove(str(work))


def test_an_asked_file_replaced_by_a_link_after_the_start_is_refused(
    kit: ModuleType, tmp_path: Path
) -> None:
    """The allowed set is where each asked file really was when the run began: one that a
    subprocess has since turned into a link to other code is no longer the file that was asked."""
    work = _delivery_tree(tmp_path)
    barrier = _barrier(kit, work, allowed={work / "test_owner.py"})
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("def test_owner():\n    assert True\n", encoding="utf-8")
    (work / "test_owner.py").unlink()
    (work / "test_owner.py").symlink_to(elsewhere)
    sys.path.insert(0, str(work))
    try:
        with pytest.raises(ImportError, match="black box"):
            barrier.find_spec("test_owner")
    finally:
        sys.path.remove(str(work))


def test_a_real_file_of_the_tree_listed_outside_it_is_refused(
    kit: ModuleType, tmp_path: Path
) -> None:
    """The other way round from a link into the tree: a module found through a directory outside
    /work, whose real file is in /work, is a delivered module. Judged by where it is listed alone,
    it would pass."""
    work = _delivery_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mirror.py").symlink_to(work / "payload.py")
    barrier = _barrier(kit, work, allowed={work / "test_owner.py"})
    sys.path.insert(0, str(outside))
    try:
        with pytest.raises(ImportError, match="black box"):
            barrier.find_spec("mirror")
    finally:
        sys.path.remove(str(outside))


def _held_to_the_manifest(kit: ModuleType, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kit as the run begins: the test asked for, and the digest the filter measured."""
    data = (work / "test_owner.py").read_bytes()
    monkeypatch.setattr(kit, "WORK", str(work))
    monkeypatch.setattr(kit, "ASKED", ["test_owner.py::test_owner", "missing.py::test_gone"])
    monkeypatch.setattr(kit, "DIGESTS", {"test_owner.py": hashlib.sha256(data).hexdigest()})
    monkeypatch.setattr(kit, "PINNED", {})


def test_the_asked_files_are_read_once_and_held_to_the_manifest(
    kit: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _delivery_tree(tmp_path)
    _held_to_the_manifest(kit, work, monkeypatch)
    kit._pin_asked_files()
    held = {str(work / "test_owner.py"): (work / "test_owner.py").read_bytes()}
    assert held == kit.PINNED


def test_an_asked_file_that_is_not_the_one_the_hub_sent_stops_the_run(
    kit: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _delivery_tree(tmp_path)
    _held_to_the_manifest(kit, work, monkeypatch)
    (work / "test_owner.py").write_text("def test_owner():\n    pass\n", encoding="utf-8")
    with pytest.raises(pytest.UsageError, match="not the one the hub sent"):
        kit._pin_asked_files()


def test_an_asked_file_the_manifest_has_no_digest_for_stops_the_run(
    kit: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filter that does not measure the files is not one this kit can be held to: fail closed."""
    work = _delivery_tree(tmp_path)
    _held_to_the_manifest(kit, work, monkeypatch)
    monkeypatch.setattr(kit, "DIGESTS", {})
    with pytest.raises(pytest.UsageError, match="not the one the hub sent"):
        kit._pin_asked_files()


def test_an_asked_file_is_compiled_from_the_bytes_held_when_the_run_began(
    kit: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is rewritten on disk after the kit read it — by a subprocess of a test that ran
    before it was imported. What pytest compiles for it is what the kit read."""
    from _pytest.assertion import rewrite

    monkeypatch.setattr(rewrite, "_rewrite_test", rewrite._rewrite_test)  # restored afterwards
    work = _delivery_tree(tmp_path)
    _held_to_the_manifest(kit, work, monkeypatch)
    kit._pin_asked_files()
    kit._install_pinned_sources()
    (work / "test_owner.py").write_text("RAN = 'the rewritten file'\n", encoding="utf-8")
    other = work / "other.py"
    other.write_text("RAN = 'a file that was not pinned'\n", encoding="utf-8")
    compile_for_pytest: Any = rewrite._rewrite_test  # without a configuration, as pytest allows
    pinned: dict[str, Any] = {}
    exec(compile_for_pytest(work / "test_owner.py", None)[1], pinned)  # noqa: S102  # reason: the code under test, compiled from a fixture
    assert "RAN" not in pinned
    assert callable(pinned["test_owner"])
    untouched: dict[str, Any] = {}
    exec(compile_for_pytest(other, None)[1], untouched)  # noqa: S102  # reason: same
    assert untouched["RAN"] == "a file that was not pinned"


def test_the_kit_refuses_to_run_with_a_pytest_it_cannot_hold_to_the_files_it_read(
    kit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _pytest.assertion import rewrite

    monkeypatch.delattr(rewrite, "_rewrite_test")
    with pytest.raises(pytest.UsageError, match="this pytest differs"):
        kit._install_pinned_sources()


def test_a_module_outside_the_delivery_tree_is_untouched(kit: ModuleType, tmp_path: Path) -> None:
    work = _delivery_tree(tmp_path)
    barrier = _barrier(kit, work, allowed=set())
    # A stdlib module resolves outside /work: the barrier does not interfere.
    assert barrier.find_spec("json") is None
    # A module that is nowhere is left to the normal machinery to reject.
    assert barrier.find_spec("zzz_nonexistent_module_name") is None


def test_the_allowed_set_is_exactly_the_existing_asked_files(
    kit: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _delivery_tree(tmp_path)
    monkeypatch.setattr(kit, "WORK", str(work))
    monkeypatch.setattr(kit, "ASKED", ["test_owner.py::test_owner", "missing.py::test_gone"])
    assert kit._allowed_paths() == {
        str(work / "test_owner.py"): str((work / "test_owner.py").resolve())
    }


def test_a_bytecode_cache_next_to_a_source_is_not_read(
    kit: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache shipped with the delivery next to an asked test file would be run in place of its
    source whenever the time and size it records match. The control shows the attack works
    here — the planted cache is what loads — and the kit's setting makes the source load."""
    monkeypatch.setattr(sys, "pycache_prefix", None)  # restored at the end of the test
    source = tmp_path / "planted.py"
    source.write_text("VALUE = 'source'\n", encoding="utf-8")
    os.utime(source, (0, 0))
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    header = importlib.util.MAGIC_NUMBER + bytes(4) + bytes(4)
    header += source.stat().st_size.to_bytes(4, "little")
    cache.write_bytes(header + marshal.dumps(compile("VALUE = 'cache'\n", str(source), "exec")))

    def loaded() -> str:
        spec = importlib.util.spec_from_file_location("planted", source)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.VALUE)

    assert loaded() == "cache"  # the control: without the kit's setting the cache wins
    kit._ignore_bytecode_caches()
    assert sys.pycache_prefix == kit.NO_BYTECODE
    assert loaded() == "source"


def test_the_kit_refuses_to_run_when_it_cannot_make_the_judge_non_dumpable(
    kit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: a judge that a subprocess could read is no judge. The refusal comes before
    anything is installed or printed, so the run has no outcome line at all."""
    monkeypatch.setattr(kit, "_set_non_dumpable", lambda: False)
    meta_path = list(sys.meta_path)
    with pytest.raises(pytest.UsageError, match="non-dumpable"):
        kit.pytest_load_initial_conftests(None, None, [])
    assert sys.meta_path == meta_path


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux's; the kit image is Linux")
def test_the_process_is_made_non_dumpable() -> None:
    """`prctl(PR_SET_DUMPABLE, 0)` is what stops a black-box test's subprocess from reading the
    judge's memory (the nonce). Checked in a fresh process, since it is a one-way change."""
    script = textwrap.dedent(
        f"""
        import ctypes, importlib.util
        spec = importlib.util.spec_from_file_location("kit", {str(KIT_FILE)!r})
        kit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kit)
        libc = ctypes.CDLL(None, use_errno=True)
        assert kit._set_non_dumpable() is True
        assert libc.prctl(3) == 0, "the process is non-dumpable afterwards"
        print("ok")
        """
    )
    result = subprocess.run(  # noqa: S603  # reason: a fixed interpreter, the script is this test's
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ----------------------------------------------------------------------------- time limits
class _Node:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


def _call(error: BaseException, when: str, duration: float) -> Any:
    try:
        raise error
    except BaseException:  # any exception, as pytest catches it
        excinfo = pytest.ExceptionInfo.from_current()
    return types.SimpleNamespace(excinfo=excinfo, when=when, duration=duration)


def _signed_payload(kit: ModuleType, line: str, mark: str) -> Any:
    assert line.startswith(mark)
    text, _, mac = line[len(mark) :].rpartition(" ")
    assert mac == hmac.new(kit._NONCE.encode(), text.encode(), hashlib.sha256).hexdigest()
    return json.loads(text)


def test_a_test_that_reaches_a_time_limit_is_named_in_the_signed_line(kit: ModuleType) -> None:
    """The cause of a test that timed out used to be in its output alone."""
    kit.OUTCOMES.clear()
    kit.OUTCOMES.update(dict.fromkeys(["t.py::slow", "t.py::shared", "t.py::other"], "error"))
    expired = subprocess.TimeoutExpired(["bench", "run"], 600)
    kit.pytest_exception_interact(_Node("t.py::slow"), _call(expired, "setup", 612.34), None)
    # a module fixture that timed out is raised again, at once, for the next test using it
    again = types.SimpleNamespace(
        excinfo=_call(expired, "setup", 0.0).excinfo, when="setup", duration=0.0
    )
    kit.pytest_exception_interact(_Node("t.py::shared"), again, None)
    kit.pytest_exception_interact(_Node("t.py::other"), _call(ValueError("no"), "call", 1), None)
    kit.pytest_exception_interact(_Node("t.py::other"), _call(TimeoutError(), "call", 3.0), None)
    kit.pytest_exception_interact(_Node("x.py::not_asked"), _call(expired, "call", 9), None)
    payload = _signed_payload(kit, kit._outcomes_line(), kit.MARK)
    assert payload["timeouts"] == {
        "t.py::slow": {"phase": "setup", "seconds": 612.3, "limit": 600.0},
        "t.py::shared": {"phase": "setup", "seconds": 612.3, "limit": 600.0},
        "t.py::other": {"phase": "call", "seconds": 3.0, "limit": None},
    }
    assert payload["outcomes"]["t.py::slow"] == "error"  # the outcome stays the kit's


def test_each_test_that_starts_is_signed_at_once(kit: ModuleType) -> None:
    kit.OUTCOMES.clear()
    kit.OUTCOMES["t.py::slow"] = "not_run"
    out = io.StringIO()
    kit.REAL_OUT.close()
    setattr(kit, "REAL_OUT", out)  # noqa: B010  # reason: the module is loaded from a file
    kit.pytest_runtest_logstart("t.py::slow", None)
    kit.pytest_runtest_logstart("x.py::not_asked", None)
    lines = [line for line in out.getvalue().splitlines() if line]
    assert out.getvalue().startswith("\n")  # whole, whatever pytest wrote before it
    assert len(lines) == 1
    payload = _signed_payload(kit, lines[0], kit.STARTED_MARK)
    assert payload["test"] == "t.py::slow"
    assert payload["at"] >= 0
    setattr(kit, "REAL_OUT", open(os.devnull, "w"))  # noqa: B010, SIM115  # reason: closed by the fixture
