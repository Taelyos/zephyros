"""Spike Z2-02 test fixture: listens on TCP and UDP ports and logs every peer that reaches it.

Usage: observer.py tcp:8080 udp:8080 ...
Every accepted connection or datagram prints one line, so a test can prove from the listener's
side that something did or did not arrive (oracle O-NET). It answers "hello from <name>".
"""

import os
import socket
import sys
import threading

NAME = os.environ.get("OBS_NAME", "observer")


def serve_tcp(port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(16)
    while True:
        conn, peer = server.accept()
        local = conn.getsockname()
        print(f"TCP {peer[0]}:{peer[1]} -> {local[0]}:{local[1]}", flush=True)
        try:
            conn.sendall(f"hello from {NAME}\n".encode())
        finally:
            conn.close()


def serve_udp(port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("0.0.0.0", port))
    while True:
        _, peer = server.recvfrom(512)
        print(f"UDP {peer[0]}:{peer[1]} -> :{port}", flush=True)
        server.sendto(f"hello from {NAME}\n".encode(), peer)


def main() -> None:
    for spec in sys.argv[1:]:
        proto, _, port = spec.partition(":")
        target = serve_tcp if proto == "tcp" else serve_udp
        threading.Thread(target=target, args=(int(port),), daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
