"""In-container probe for spike Z2-01: where can this user create a file?

Tries to create (and remove) a file in the root of every mount and in every directory of the
root file system that the access check says is writable. Prints one path per line.
"""

import os
import uuid


def can_create(directory):
    name = os.path.join(directory, f".zs-{uuid.uuid4().hex[:8]}")
    try:
        os.close(os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return False
    os.unlink(name)
    return True


with open("/proc/self/mountinfo", encoding="utf-8") as handle:
    mounts = [line.split()[4] for line in handle]

found = set()
for mount in mounts:
    if mount.startswith(("/proc", "/sys")):
        continue
    if os.path.isdir(mount) and can_create(mount):
        found.add(mount)

# The root file system itself: every directory on it, not crossing into other mounts.
root_dev = os.stat("/").st_dev
for current, dirs, _files in os.walk("/", topdown=True):
    dirs[:] = [
        d
        for d in dirs
        if not os.path.ismount(os.path.join(current, d))
        and os.lstat(os.path.join(current, d)).st_dev == root_dev
        and not os.path.islink(os.path.join(current, d))
    ]
    if os.lstat(current).st_dev == root_dev and os.access(current, os.W_OK) and can_create(current):
        found.add(current)

for path in sorted(found):
    print(path)
