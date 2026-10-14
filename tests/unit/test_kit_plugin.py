"""The tests kit's plugin, unit-tested without a container (ATK-22, ADR 0017).

The plugin keeps delivered code out of the judging process: an import barrier that refuses every
/work module but the asked test files, and a non-dumpable process. These are the two mechanisms
the forgery defence rests on, so they are checked here directly; the end-to-end behaviour is in
tests/runtime/test_sandbox_runner_conforms.py.
"""

from __future__ import annotations

import importlib.util
import marshal
import os
import subprocess
import sys
import textwrap
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
