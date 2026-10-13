"""In-container probe for spike Z2-01: processes, listening sockets, mounts, memory, limits.

Prints one JSON object. Reads /proc and the cgroup only; runs as any user.
"""

import json
import os


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def processes():
    rows = []
    for entry in sorted((e for e in os.listdir("/proc") if e.isdigit()), key=int):
        status = read(f"/proc/{entry}/status")
        if status is None:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        cmd = (read(f"/proc/{entry}/cmdline") or "").replace("\0", " ").strip()
        rows.append(
            {
                "pid": int(entry),
                "ppid": int(fields["PPid"]),
                "name": fields["Name"].strip(),
                "threads": int(fields["Threads"]),
                "rss_kb": int(fields.get("VmRSS", "0 kB").split()[0]),
                "cmd": cmd[:120],
            }
        )
    return rows


def hex_addr(raw, v6):
    host, port = raw.split(":")
    if v6:
        words = [host[i : i + 8] for i in range(0, 32, 8)]
        octets = b"".join(bytes.fromhex(w)[::-1] for w in words)
        groups = [octets[i : i + 2].hex() for i in range(0, 16, 2)]
        return f"[{':'.join(groups)}]:{int(port, 16)}"
    octets = bytes.fromhex(host)[::-1]
    return f"{'.'.join(str(b) for b in octets)}:{int(port, 16)}"


def listeners():
    found = []
    for name, v6 in (("tcp", False), ("tcp6", True), ("udp", False), ("udp6", True)):
        text = read(f"/proc/net/{name}") or ""
        for line in text.splitlines()[1:]:
            parts = line.split()
            state = parts[3]
            # TCP: 0A is LISTEN. UDP: 07 is an unconnected bound socket.
            if (name.startswith("tcp") and state == "0A") or (
                name.startswith("udp") and state == "07"
            ):
                found.append(f"{name} {hex_addr(parts[1], v6)}")
    unix = []
    for line in (read("/proc/net/unix") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 8 and parts[3] == "00010000":  # SO_ACCEPTCON: a listening socket
            unix.append(f"unix {parts[7]}")
    return found, unix


def writable_mounts():
    rows = []
    for line in (read("/proc/self/mountinfo") or "").splitlines():
        left, right = line.split(" - ")
        fields = left.split()
        options = fields[5].split(",")
        if "rw" in options:
            rows.append(f"{fields[4]} ({right.split()[0]})")
    return rows


def cgroup():
    out = {}
    for name in (
        "memory.current",
        "memory.peak",
        "memory.max",
        "pids.current",
        "pids.peak",
        "pids.max",
    ):
        value = read(f"/sys/fs/cgroup/{name}")
        out[name] = value.strip() if value else None
    return out


tcp_udp, unix = listeners()
procs = processes()
print(
    json.dumps(
        {
            "uid": os.getuid(),
            "processes": procs,
            "process_count": len(procs),
            "thread_count": sum(p["threads"] for p in procs),
            "listeners": tcp_udp,
            "unix_listeners": unix,
            "writable_mounts": writable_mounts(),
            "cgroup": cgroup(),
            "capeff": next(
                (
                    row.split()[1]
                    for row in (read("/proc/self/status") or "").splitlines()
                    if row.startswith("CapEff")
                ),
                None,
            ),
            "seccomp": next(
                (
                    row.split()[1]
                    for row in (read("/proc/self/status") or "").splitlines()
                    if row.startswith("Seccomp:")
                ),
                None,
            ),
            "no_new_privs": next(
                (
                    row.split()[1]
                    for row in (read("/proc/self/status") or "").splitlines()
                    if row.startswith("NoNewPrivs")
                ),
                None,
            ),
        },
        indent=1,
    )
)
