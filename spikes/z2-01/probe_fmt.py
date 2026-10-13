"""Host-side formatter for the JSON that probe.py prints inside an instance (spike Z2-01).

docker exec NAME python3 /spike/probe.py | python3 probe_fmt.py COMMAND
"""

from __future__ import annotations

import json
import sys

RESOLVER = "127.0.0.11"  # the runtime's embedded DNS, a part of the network, not of the instance


def main() -> int:
    data = json.load(sys.stdin)
    command = sys.argv[1]
    own = [x for x in data["listeners"] if RESOLVER not in x]
    if command == "listeners":
        print(", ".join(own + data["unix_listeners"]) or "none")
    elif command == "tcp-listeners":
        print(", ".join(x for x in own if x.startswith("tcp")) or "none")
    elif command == "summary":
        cg = data["cgroup"]
        print(
            f"processes={data['process_count']} threads={data['thread_count']} "
            f"memory.current={int(cg['memory.current']) // 2**20}MiB "
            f"memory.peak={int(cg['memory.peak']) // 2**20}MiB "
            f"pids.current={cg['pids.current']} pids.peak={cg['pids.peak']}"
        )
    elif command == "processes":
        for p in data["processes"]:
            print(
                f"  pid {p['pid']:>3} ppid {p['ppid']:>3} threads {p['threads']:>2} "
                f"rss {p['rss_kb'] // 1024:>4}MiB  {p['name']}: {p['cmd'][:70]}"
            )
    elif command == "caps":
        print(
            f"uid={data['uid']} CapEff={data['capeff']} NoNewPrivs={data['no_new_privs']} "
            f"Seccomp={data['seccomp']}"
        )
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
