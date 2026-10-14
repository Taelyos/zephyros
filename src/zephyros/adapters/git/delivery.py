"""The staging area as git: one bare repository per project, one branch per delivery.

The base is `refs/heads/base` (and HEAD), so the package of the base clones into a working copy.
A delivery is a bundle that carries one state (its refs, HEAD and a branch for instance, all
point at one commit); it is fetched into `refs/deliveries/<branch>` with object checks, and kept
only when it descends from the base.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

from zephyros.adapters.git.run import (
    GitError,
    bundle,
    git,
    heads_of_bundle,
    init_bare,
    lines,
    rev,
    write_package,
)
from zephyros.core.ports.delivery import (
    BRANCH,
    INTERFACE_VERSION,
    MAX_PACKAGE,
    Delivery,
    DeliveryError,
    Tree,
)

MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_FILES = 20_000
_PROJECT = re.compile(r"^p_[a-z0-9]{6,32}$")
_REGULAR = ("100644", "100755")


class GitDeliveryArea:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    # ------------------------------------------------------------------ the interface
    def prepare(self, project_id: str, package: bytes, branch: str) -> str:
        repository = self._repository(project_id, create=True)
        with self._lock, tempfile.TemporaryDirectory(dir=self._root) as scratch:
            path = self._incoming(Path(scratch), package)
            heads = self._heads(repository, path)
            ref = f"refs/heads/{branch}"
            if ref not in heads:
                raise DeliveryError("the package does not carry that branch")
            self._git(
                "fetch",
                "--quiet",
                "--no-tags",
                str(path),
                f"+{ref}:refs/heads/base",
                cwd=repository,
            )
            self._git("symbolic-ref", "HEAD", "refs/heads/base", cwd=repository)
            return rev(repository, "refs/heads/base")

    def base_package(self, project_id: str) -> bytes:
        repository = self._repository(project_id)
        with self._lock:
            return self._bundle(repository, ["HEAD", "base"])

    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        repository = self._repository(project_id)
        if not BRANCH.fullmatch(branch):
            raise DeliveryError("the branch name is not plain")
        with self._lock, tempfile.TemporaryDirectory(dir=self._root) as scratch:
            index = self._index(repository)
            if any(entry["branch"] == branch for entry in index.values()):
                raise DeliveryError("this branch was already delivered")
            path = self._incoming(Path(scratch), package)
            heads = self._heads(repository, path)
            if len(set(heads.values())) != 1:
                raise DeliveryError("the package must carry one state: the delivered branch")
            source = min(heads, key=lambda ref: (not ref.startswith("refs/heads/"), ref))
            target = f"refs/deliveries/{branch}"
            self._git(
                "fetch", "--quiet", "--no-tags", str(path), f"{source}:{target}", cwd=repository
            )
            base = rev(repository, "refs/heads/base")
            head = rev(repository, target)
            ancestor = git("merge-base", "--is-ancestor", base, head, cwd=repository, check=False)
            if ancestor.returncode != 0 or head == base:
                git("update-ref", "-d", target, cwd=repository, check=False)
                raise DeliveryError("the delivery does not descend from the base")
            changed = tuple(
                sorted(
                    lines(
                        git(
                            "diff", "--name-only", "-z", "--no-renames", base, head, cwd=repository
                        ).stdout,
                        nul=True,
                    )
                )
            )
            delivery_id = f"d{len(index) + 1}"
            index[delivery_id] = {
                "branch": branch,
                "base": base,
                "head": head,
                "changed": list(changed),
            }
            self._save_index(repository, index)
            return Delivery(project_id, delivery_id, branch, base, head, changed)

    def get(self, project_id: str, delivery_id: str) -> Delivery:
        entry = self._entry(project_id, delivery_id)
        changed = entry["changed"]
        return Delivery(
            project_id,
            delivery_id,
            str(entry["branch"]),
            str(entry["base"]),
            str(entry["head"]),
            tuple(str(p) for p in changed) if isinstance(changed, list) else (),
        )

    def tree(self, project_id: str, ref: str) -> Tree:
        repository = self._repository(project_id)
        commit = (
            rev(repository, "refs/heads/base")
            if ref == "base"
            else str(self._entry(project_id, ref)["head"])
        )
        # The listing carries the size of each blob (`-l`): the limits are checked before a
        # single byte of content is read, and the content comes from one git for the whole tree.
        listing = git("ls-tree", "-r", "-z", "-l", "--full-tree", commit, cwd=repository).stdout
        wanted: list[tuple[str, str, int]] = []
        for entry in lines(listing, nul=True):
            meta, _, path = entry.partition("\t")
            mode, kind, sha, size = meta.split()
            if kind == "blob" and mode in _REGULAR:
                wanted.append((path, sha, int(size)))
        if len(wanted) > MAX_FILES:
            raise DeliveryError("the tree has too many files")
        if sum(size for _, _, size in wanted) > MAX_TREE_BYTES:
            raise DeliveryError("the tree is too large")
        blobs = _read_blobs(repository, {sha: size for _, sha, size in wanted})
        return Tree({path: blobs[sha] for path, sha, _ in wanted})

    def package(self, project_id: str, delivery_id: str) -> bytes:
        repository = self._repository(project_id)
        entry = self._entry(project_id, delivery_id)
        with self._lock:
            return self._bundle(repository, [f"refs/deliveries/{entry['branch']}"])

    # ------------------------------------------------------------------ helpers
    def _repository(self, project_id: str, *, create: bool = False) -> Path:
        if not _PROJECT.fullmatch(project_id):
            raise DeliveryError("not a project identifier")
        path = self._root / f"{project_id}.git"
        if create:
            init_bare(path)
        elif not (path / "HEAD").exists() or not self._has_base(path):
            raise DeliveryError("the project has no staging area")
        return path

    @staticmethod
    def _has_base(path: Path) -> bool:
        return (
            git(
                "rev-parse", "--verify", "--quiet", "refs/heads/base", cwd=path, check=False
            ).returncode
            == 0
        )

    @staticmethod
    def _incoming(scratch: Path, package: bytes) -> Path:
        if len(package) > MAX_PACKAGE:
            raise DeliveryError("the package is too large")
        return write_package(scratch, package)

    def _heads(self, repository: Path, path: Path) -> dict[str, str]:
        try:
            return heads_of_bundle(repository, path)
        except GitError:
            raise DeliveryError("the package cannot be read") from None

    @staticmethod
    def _git(*args: str, cwd: Path) -> None:
        try:
            git(*args, cwd=cwd)
        except GitError as error:
            raise DeliveryError(str(error)) from None

    @staticmethod
    def _bundle(repository: Path, refs: list[str]) -> bytes:
        try:
            return bundle(repository, refs)
        except GitError as error:
            raise DeliveryError(str(error)) from None

    @staticmethod
    def _index(repository: Path) -> dict[str, dict[str, object]]:
        path = repository / "zephyros-deliveries.json"
        if not path.exists():
            return {}
        data: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
        return data

    @staticmethod
    def _save_index(repository: Path, index: dict[str, dict[str, object]]) -> None:
        path = repository / "zephyros-deliveries.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def _entry(self, project_id: str, delivery_id: str) -> dict[str, object]:
        entry = self._index(self._repository(project_id)).get(delivery_id)
        if entry is None:
            raise DeliveryError("there is no such delivery")
        return entry


def _read_blobs(repository: Path, sizes: Mapping[str, int]) -> dict[str, bytes]:
    """The content of each blob, from one `git cat-file --batch`. git answers each name with a
    header `<name> blob <size>`, the content and a newline, in the order asked. The content is cut
    by its size, never by a separator it may hold itself; anything else than the exact objects
    asked for, at the size the listing gave, is refused."""
    if not sizes:
        return {}
    request = "".join(f"{sha}\n" for sha in sizes).encode()
    output = git("cat-file", "--batch", cwd=repository, input_bytes=request).stdout
    blobs: dict[str, bytes] = {}
    at = 0
    for sha, size in sizes.items():
        end = output.find(b"\n", at)
        start = end + 1
        stop = start + size
        if (
            end < 0
            or output[at:end] != f"{sha} blob {size}".encode()
            or output[stop : stop + 1] != b"\n"
        ):
            raise DeliveryError("the tree cannot be read")
        blobs[sha] = output[start:stop]
        at = stop + 1
    if at != len(output):
        raise DeliveryError("the tree cannot be read")
    return blobs
