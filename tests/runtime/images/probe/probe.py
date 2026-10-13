#!/usr/bin/env python3
"""Connection attempts from inside an instance, in parallel, with a short timeout.

Usage: probe connect '<json list of {"proto", "host", "port"}>'
       probe fetch URL

`connect` prints a JSON list of results, "reached" or "blocked" with the error. UDP counts as
reached only when the listener answers. `fetch` follows redirects and prints the final outcome.
"""

import json
import socket
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 2.0


def attempt(target: dict[str, object]) -> dict[str, object]:
    proto, host, port = str(target["proto"]), str(target["host"]), int(str(target["port"]))
    try:
        if proto == "tcp":
            with socket.create_connection((host, port), timeout=TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                try:
                    data = conn.recv(64)
                except OSError:
                    data = b""
            return {**target, "result": "reached", "answer": data.decode(errors="replace")}
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as udp:
            udp.settimeout(TIMEOUT)
            udp.sendto(b"ping", (host, port))
            data, _ = udp.recvfrom(64)
        return {**target, "result": "reached", "answer": data.decode(errors="replace")}
    except OSError as error:
        return {**target, "result": "blocked", "error": type(error).__name__}


def main() -> None:
    if sys.argv[1] == "connect":
        targets = json.loads(sys.argv[2])
        with ThreadPoolExecutor(max_workers=32) as pool:
            print(json.dumps(list(pool.map(attempt, targets))))
    elif sys.argv[1] == "fetch":
        try:
            with urllib.request.urlopen(sys.argv[2], timeout=TIMEOUT) as response:  # noqa: S310
                print(json.dumps({"result": "reached", "status": response.status}))
        except OSError as error:
            print(json.dumps({"result": "blocked", "error": type(error).__name__}))


if __name__ == "__main__":
    main()
