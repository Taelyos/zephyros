"""The tools directory of a project, read on the file system of the hub (ADR 0030).

`ToolsDirectory` walks the directory without following a link, refuses what the tools of a
project may not hold, and fingerprints the rest. The hub reads the directory at the path the
container runtime binds (`tools.source`): `scripts/deploy.sh` mounts it into the hub container,
read-only, at that same path.

What is refused, each with the path it is found at:

- an entry that the user of the instances (uid and gid 10000) could change: one it owns, one its
  group may write, one every user may write, one that carries an access control list (which this
  check cannot read for who it lets write);
- a link to an absolute path (inside a container it would point at something else) or one that
  leads out of the directory: on the machine (`realpath`), or on its way, step by step, as the
  kernel reads it in a container where the directory is the root of the mount (a `..` above the
  directory is a way out, even when the next names lead back in on the machine);
- a file system mounted under the directory: the mount is not recursive, so the turns and the
  sandbox would find an empty directory where the hub read files;
- anything but a directory, a regular file or a link (a device, a socket, a pipe), a setuid or
  setgid file, and a file the hub cannot read (it could not be fingerprinted).

The fingerprint is the SHA-256 of one JSON line per entry, sorted by path: a directory
`["d", path]`, a file `["f", path, "x" or "-", size, sha256 of its content]` (`x` when any of
its execute bits is set), a link `["l", path, target]`. Paths are relative to the directory, with
`/` between names; the directory itself is `""`. Owners, other mode bits and times do not count,
so the same tree copied elsewhere has the same fingerprint (`zephyros tools check DIR`).

Hashing hundreds of megabytes takes a second or two, so the result is kept with the status of
every entry (size, times, inode, mode, owner): it is computed again when any of them changed. A
change of a file's content changes its change time (ctime), which no program sets at will; but
the kernel moves the ctime by ticks of a few milliseconds, so a change made in the tick of the
hashing could leave every status as it was. A result is therefore kept only when every entry
changed at least `RACY_SECONDS` before the walk began, as git does for its index.

The problems name paths relative to the directory, never its path on the machine: they reach the
log of the project and the status of a delivery, which the agents read. The operator finds the
path in the message of the error (`str`), in the log of the hub and in the configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from zephyros.core.containers import INSTANCE_USER
from zephyros.core.tools import ToolsDigest, ToolsError

CHUNK = 1024 * 1024
ACL_ATTRIBUTES = ("system.posix_acl_access", "system.posix_acl_default")
_UID, _GID = (int(part) for part in INSTANCE_USER.split(":"))
MAX_LINKS = 40
"""The links one path may cross before the kernel gives up (ELOOP); a longer chain is refused."""
RACY_SECONDS = 2
"""How long ago every entry must have changed for a fingerprint to be kept (see above)."""
MOUNTINFO = "/proc/self/mountinfo"


@dataclass(frozen=True, slots=True)
class _Entry:
    path: str
    """Relative to the root, `/` between names; the root is ""."""
    full: str
    status: os.stat_result
    target: str | None = None
    """What a link points at, as written in the link."""


def _relative(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _walk(root: str) -> Iterator[_Entry]:
    """Every entry under `root`, root included, never through a link."""
    pending = [("", root)]
    while pending:
        relative, full = pending.pop()
        status = os.lstat(full)
        target = os.readlink(full) if stat.S_ISLNK(status.st_mode) else None
        yield _Entry(relative, full, status, target)
        if stat.S_ISDIR(status.st_mode):
            with os.scandir(full) as listing:
                names = sorted(entry.name for entry in listing)
            pending.extend(
                (_relative(relative, name), os.path.join(full, name)) for name in reversed(names)
            )


def _shown(path: str) -> str:
    return repr(path or ".")


def _has_acl(full: str) -> bool:
    listxattr = getattr(os, "listxattr", None)
    if (
        listxattr is None
    ):  # pragma: no cover  # reason: macOS has no listxattr; the hub runs on Linux
        return False
    try:
        names = listxattr(full, follow_symlinks=False)
    except OSError:
        return False  # a file system without extended attributes has no ACL either
    return any(name in ACL_ATTRIBUTES for name in names)


def _climbs_out(root: str, path: str, target: str) -> bool | None:
    """Whether the link at `path` climbs above the root on its way, read step by step as the
    kernel reads it inside a container, where the root is that of the mount: a `..` there stays
    at `/opt/project-tools/..`, wherever the following names would lead back on the machine. A
    link met on the way is followed from where it is; a step that does not exist is read as a
    name, which can only make the check stricter. None when the chain of links is too long for
    the kernel to follow (a loop)."""
    parts = path.split("/")[:-1]  # the directory that holds the link
    pending = target.split("/")
    followed = 0
    while pending:
        name = pending.pop(0)
        if name in ("", "."):
            continue
        if name == "..":
            if not parts:
                return True
            parts.pop()
            continue
        parts.append(name)
        full = os.path.join(root, *parts)
        try:
            status = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISLNK(status.st_mode):
            followed += 1
            if followed > MAX_LINKS:
                return None
            inner = os.readlink(full)
            if os.path.isabs(inner):
                return True
            parts.pop()
            pending[:0] = inner.split("/")
    return False


def _unescape(field: str) -> str:
    """A path of /proc/self/mountinfo, where a space, a tab, a newline and a backslash are written
    in octal."""
    return re.sub(r"\\([0-7]{3})", lambda found: chr(int(found.group(1), 8)), field)


def _mount_points(root: str, mountinfo: str) -> list[str]:
    """The mount points strictly under `root` (resolved), relative to it; none where the system
    keeps no mountinfo (macOS, where `zephyros tools check` may run on a copy)."""
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 5:  # the mount point is the fifth field
            continue
        point = _unescape(fields[4])
        if point.startswith(root.rstrip("/") + "/"):
            found.append(point[len(root.rstrip("/")) + 1 :])
    return sorted(set(found))


def _problems(root: str, entry: _Entry) -> list[str]:
    mode = entry.status.st_mode
    where = _shown(entry.path)
    if stat.S_ISLNK(mode):
        target = entry.target or ""
        if os.path.isabs(target):
            return [f"{where} is a link to an absolute path; in a container it points elsewhere"]
        resolved = os.path.realpath(entry.full)
        climbs = _climbs_out(root, entry.path, target)
        if climbs is None:
            return [f"{where} is a link in a loop, or behind too many links"]
        if climbs or os.path.commonpath([resolved, root]) != root:
            return [f"{where} is a link that leads out of the tools directory"]
        return []
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        return [f"{where} is neither a directory, a regular file nor a link"]
    found: list[str] = []
    if entry.status.st_uid == _UID:
        found.append(f"{where} belongs to the user of the instances (uid {_UID})")
    if entry.status.st_gid == _GID and mode & stat.S_IWGRP:
        found.append(f"{where} is writable by the group of the instances (gid {_GID})")
    if mode & stat.S_IWOTH:
        found.append(f"{where} is writable by every user")
    if mode & (stat.S_ISUID | stat.S_ISGID) and stat.S_ISREG(mode):
        found.append(f"{where} is setuid or setgid")
    if _has_acl(entry.full):
        found.append(f"{where} carries an access control list")
    return found


def _line(item: list[object]) -> bytes:
    return (json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _content_digest(full: str) -> str:
    digest = hashlib.sha256()
    with open(full, "rb") as handle:  # reason: the walk works on str paths
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class ToolsDirectory:
    """The tools directory at `root`, measured each time `digest` is called."""

    def __init__(
        self,
        root: Path,
        *,
        mountinfo: str = MOUNTINFO,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._root = str(root)
        self._mountinfo = mountinfo
        self._now = now
        self._lock = threading.Lock()
        self._kept: tuple[str, ToolsDigest] | None = None

    @property
    def root(self) -> str:
        return self._root

    def digest(self) -> ToolsDigest:
        with self._lock:
            began = self._now()
            entries = self._entries()
            signature = self._signature(entries)
            if self._kept is not None and self._kept[0] == signature:
                return self._kept[1]
            measured = self._fingerprint(entries)
            newest = max(entry.status.st_ctime for entry in entries)
            self._kept = (signature, measured) if newest < began - RACY_SECONDS else None
            return measured

    def _entries(self) -> list[_Entry]:
        root = self._root
        if not os.path.isabs(root):
            raise ToolsError(["the tools directory is not named by an absolute path"], root)
        try:
            status = os.lstat(root)
        except OSError as error:
            raise ToolsError(
                [f"the tools directory cannot be read: {error.strerror or error}"], root
            ) from None
        if stat.S_ISLNK(status.st_mode):
            raise ToolsError(
                ["the tools directory is a link: name the directory it points at"], root
            )
        if not stat.S_ISDIR(status.st_mode):
            raise ToolsError(["the tools directory is not a directory"], root)
        resolved = os.path.realpath(root)
        entries: list[_Entry] = []
        problems = [
            f"{_shown(point)} is a mount point: the turns and the sandbox would find it empty"
            for point in _mount_points(resolved, self._mountinfo)
        ]
        try:
            for entry in _walk(root):
                entries.append(entry)
                problems.extend(_problems(resolved, entry))
        except OSError as error:
            problems.append(
                f"{self._inside(error.filename)} cannot be read: {error.strerror or error}"
            )
        if problems:
            raise ToolsError(problems, root)
        entries.sort(key=lambda entry: os.fsencode(entry.path))
        return entries

    def _inside(self, filename: object) -> str:
        """A path the walk failed on, relative to the directory: never its path on the machine."""
        full = os.fsdecode(filename) if isinstance(filename, (str, bytes)) else ""
        if full == self._root:
            return _shown("")
        if full.startswith(self._root.rstrip("/") + "/"):
            return _shown(full[len(self._root.rstrip("/")) + 1 :])
        return "an entry"

    @staticmethod
    def _signature(entries: list[_Entry]) -> str:
        digest = hashlib.sha256()
        for entry in entries:
            s = entry.status
            digest.update(
                _line(
                    [
                        entry.path,
                        entry.target,
                        s.st_mode,
                        s.st_uid,
                        s.st_gid,
                        s.st_size,
                        s.st_mtime_ns,
                        s.st_ctime_ns,
                        s.st_ino,
                        s.st_dev,
                    ]
                )
            )
        return digest.hexdigest()

    def _fingerprint(self, entries: list[_Entry]) -> ToolsDigest:
        digest = hashlib.sha256()
        files = size = 0
        problems: list[str] = []
        for entry in entries:
            mode = entry.status.st_mode
            if stat.S_ISDIR(mode):
                digest.update(_line(["d", entry.path]))
            elif stat.S_ISLNK(mode):
                digest.update(_line(["l", entry.path, entry.target]))
            else:
                try:
                    content = _content_digest(entry.full)
                except OSError as error:
                    problems.append(
                        f"{_shown(entry.path)} cannot be read: {error.strerror or error}"
                    )
                    continue
                executable = "x" if mode & 0o111 else "-"
                digest.update(_line(["f", entry.path, executable, entry.status.st_size, content]))
                files += 1
                size += entry.status.st_size
        if problems:
            raise ToolsError(problems, self._root)
        return ToolsDigest("sha256:" + digest.hexdigest(), files, size)
