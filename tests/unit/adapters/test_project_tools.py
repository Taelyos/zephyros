"""ADR 0030: the tools directory as the hub reads it. What the user of the instances could change,
a link that leads out, and what cannot be fingerprinted are refused, each path named; the
fingerprint follows the content and the execute bits, nothing else, and is computed again only
when an entry changed."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from zephyros.adapters import project_tools
from zephyros.adapters.project_tools import ToolsDirectory
from zephyros.core.tools import MAX_PROBLEMS, ToolsDigest, ToolsError

SIM = b"\x7fELF a simulator, or what stands for one\n"
SCORE = b"#!/bin/sh\necho 42\n"


def make_tools(root: Path) -> Path:
    """A small tools tree, as an operator would leave it: 0755 directories, 0644 files, 0755
    programs, a relative link inside."""
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "bin" / "sim").write_bytes(SIM)
    (root / "bin" / "score").write_bytes(SCORE)
    (root / "lib" / "levels.txt").write_bytes(b"1\n2\n3\n")
    (root / "bin" / "levels").symlink_to("../lib/levels.txt")
    for path in (root, root / "bin", root / "lib"):
        path.chmod(0o755)
    for path in (root / "bin" / "sim", root / "bin" / "score"):
        path.chmod(0o755)
    (root / "lib" / "levels.txt").chmod(0o644)
    return root


def problems(root: Path) -> list[str]:
    with pytest.raises(ToolsError) as refused:
        ToolsDirectory(root).digest()
    return list(refused.value.problems)


def test_a_tree_of_the_operator_is_fingerprinted_with_its_size(tmp_path: Path) -> None:
    measured = ToolsDirectory(make_tools(tmp_path / "tools")).digest()
    assert measured.fingerprint.startswith("sha256:")
    assert len(measured.fingerprint) == len("sha256:") + 64
    assert (measured.files, measured.bytes) == (3, len(SIM) + len(SCORE) + 6)
    assert measured.short == measured.fingerprint[:23]


def test_the_same_tree_elsewhere_has_the_same_fingerprint(tmp_path: Path) -> None:
    """A verdict can be replayed on another machine: owners, times and the place of the
    directory do not count."""
    first = make_tools(tmp_path / "one")
    second = tmp_path / "elsewhere" / "two"
    shutil.copytree(first, second, symlinks=True)
    os.utime(second / "bin" / "sim", (1, 1))
    (second / "lib" / "levels.txt").chmod(0o640)  # a mode bit that is not an execute bit
    assert ToolsDirectory(first).digest() == ToolsDirectory(second).digest()


@pytest.mark.parametrize(
    "change",
    [
        lambda root: (root / "bin" / "sim").write_bytes(SIM + b"!"),
        lambda root: (root / "bin" / "score").chmod(0o644),  # no longer runs
        lambda root: (root / "bin" / "score").rename(root / "bin" / "scorer"),
        lambda root: (root / "lib" / "more").mkdir(mode=0o755),
        lambda root: (
            (root / "bin" / "levels").unlink(),
            (root / "bin" / "levels").symlink_to("sim"),
        ),
        lambda root: (root / "lib" / "levels.txt").unlink(),
    ],
)
def test_any_change_of_content_name_execute_bit_or_link_changes_the_fingerprint(
    tmp_path: Path, change: object
) -> None:
    root = make_tools(tmp_path / "tools")
    tools = ToolsDirectory(root)
    before = tools.digest()
    change(root)  # type: ignore[operator]  # reason: each parameter is a callable
    assert tools.digest().fingerprint != before.fingerprint


def test_an_unchanged_tree_is_not_hashed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_tools(tmp_path / "tools")
    hashed: list[str] = []
    real = project_tools._content_digest

    def counting(full: str) -> str:
        hashed.append(full)
        return real(full)

    monkeypatch.setattr(project_tools, "_content_digest", counting)
    tools = ToolsDirectory(root)
    first = tools.digest()
    assert len(hashed) == 3
    assert tools.digest() is first
    assert len(hashed) == 3
    (root / "bin" / "sim").write_bytes(SIM + b"?")  # the change time moves with the content
    assert tools.digest() != first
    assert len(hashed) == 6


def test_an_empty_directory_has_tools_too(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir(mode=0o755)
    assert ToolsDirectory(root).digest() == ToolsDigest(
        ToolsDirectory(root).digest().fingerprint, 0, 0
    )


# ----------------------------------------------------------------------------- refusals
def test_what_every_user_may_write_is_refused(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    (root / "bin" / "sim").chmod(0o757)
    (root / "lib").chmod(0o777)
    assert problems(root) == [
        "'bin/sim' is writable by every user",
        "'lib' is writable by every user",
    ]


def test_an_absolute_link_is_refused_even_to_a_file_inside(tmp_path: Path) -> None:
    """In a container the directory is at /opt/project-tools, not at its path on the machine: an
    absolute link would point at something else there."""
    root = make_tools(tmp_path / "tools")
    (root / "bin" / "abs").symlink_to(root / "bin" / "sim")
    assert problems(root) == [
        "'bin/abs' is a link to an absolute path; in a container it points elsewhere"
    ]


@pytest.mark.parametrize("target", ["../../outside", "../..", "../lib/../../outside/x"])
def test_a_link_that_leads_out_is_refused(tmp_path: Path, target: str) -> None:
    root = make_tools(tmp_path / "tools")
    (tmp_path / "outside").mkdir()
    (root / "bin" / "out").symlink_to(target)
    assert problems(root) == ["'bin/out' is a link that leads out of the tools directory"]


def test_a_link_through_a_link_that_leads_out_is_refused(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    (tmp_path / "outside").mkdir()
    (root / "lib" / "up").symlink_to("../..")
    (root / "bin" / "far").symlink_to("../lib/up/outside")
    assert sorted(problems(root)) == [
        "'bin/far' is a link that leads out of the tools directory",
        "'lib/up' is a link that leads out of the tools directory",
    ]


def test_a_dangling_link_inside_is_kept(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    (root / "bin" / "later").symlink_to("../lib/not-yet")
    assert ToolsDirectory(root).digest().files == 3


def test_a_pipe_is_refused(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    os.mkfifo(root / "lib" / "pipe", 0o644)
    assert problems(root) == ["'lib/pipe' is neither a directory, a regular file nor a link"]


def test_a_setuid_or_setgid_program_is_refused(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    (root / "bin" / "sim").chmod(0o4755)
    (root / "bin" / "score").chmod(0o2755)
    assert problems(root) == ["'bin/score' is setuid or setgid", "'bin/sim' is setuid or setgid"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_a_file_the_hub_cannot_read_is_refused(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    (root / "lib" / "levels.txt").chmod(0o000)
    try:
        assert problems(root) == ["'lib/levels.txt' cannot be read: Permission denied"]
    finally:
        (root / "lib" / "levels.txt").chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_a_directory_the_hub_cannot_list_is_refused(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    (root / "lib").chmod(0o000)
    try:
        found = problems(root)
    finally:
        (root / "lib").chmod(0o755)
    assert len(found) == 1
    assert found[0].endswith("cannot be read: Permission denied")


def status_of(
    real: os.stat_result, *, uid: int | None = None, gid: int | None = None
) -> os.stat_result:
    fields = list(real)
    if uid is not None:
        fields[stat.ST_UID] = uid
    if gid is not None:
        fields[stat.ST_GID] = gid
    return os.stat_result(fields)


def test_what_the_user_or_the_group_of_the_instances_could_change_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without root, a test cannot give a file to uid 10000: the walk sees it so through a
    status that says so."""
    root = make_tools(tmp_path / "tools")
    (root / "lib" / "levels.txt").chmod(0o664)
    real_lstat = os.lstat

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        name, found = os.fspath(path), real_lstat(path)
        if name.endswith("bin/sim"):
            return status_of(found, uid=10000)
        if name.endswith(("levels.txt", "bin/score")):  # bin/score is 0755: the group reads it
            return status_of(found, gid=10000)
        return found

    monkeypatch.setattr(os, "lstat", lstat)
    assert problems(root) == [
        "'bin/sim' belongs to the user of the instances (uid 10000)",
        "'lib/levels.txt' is writable by the group of the instances (gid 10000)",
    ]


def test_an_access_control_list_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_tools(tmp_path / "tools")

    def listxattr(path: str, *, follow_symlinks: bool = True) -> list[str]:
        assert follow_symlinks is False
        return ["system.posix_acl_access"] if path.endswith("bin") else []

    monkeypatch.setattr(os, "listxattr", listxattr, raising=False)
    assert problems(root) == ["'bin' carries an access control list"]


def test_a_file_system_without_extended_attributes_has_no_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def listxattr(path: str, *, follow_symlinks: bool = True) -> list[str]:
        raise OSError(95, "Operation not supported", path)

    monkeypatch.setattr(os, "listxattr", listxattr, raising=False)
    assert ToolsDirectory(make_tools(tmp_path / "tools")).digest().files == 3


def missing(tmp: Path) -> Path:
    return tmp / "missing"


def a_file(tmp: Path) -> Path:
    (tmp / "file").write_bytes(b"x")
    return tmp / "file"


def a_link(tmp: Path) -> Path:
    make_tools(tmp / "real")
    (tmp / "link").symlink_to(tmp / "real")
    return tmp / "link"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (missing, "cannot be read: No such file or directory"),
        (a_file, "is not a directory"),
        (a_link, "is a link: name the directory it points at"),
    ],
)
def test_a_root_that_is_not_a_directory_is_refused(
    tmp_path: Path, make: Callable[[Path], Path], message: str
) -> None:
    root = make(tmp_path)
    (refusal,) = problems(root)
    assert refusal == f"{str(root)!r} {message}"


def test_a_relative_root_is_refused() -> None:
    assert problems(Path("tools")) == ["'tools' is not an absolute path"]


def test_a_refusal_names_the_first_problems_and_counts_the_others(tmp_path: Path) -> None:
    root = make_tools(tmp_path / "tools")
    for number in range(MAX_PROBLEMS + 5):
        (root / "lib" / f"f{number:02}").write_bytes(b"x")
        (root / "lib" / f"f{number:02}").chmod(0o666)
    with pytest.raises(ToolsError) as refused:
        ToolsDirectory(root).digest()
    assert len(refused.value.problems) == MAX_PROBLEMS + 5
    text = str(refused.value)
    assert text.startswith("the tools directory cannot be mounted:\n  'lib/f00' is writable")
    assert text.endswith("\n  and 5 more")
