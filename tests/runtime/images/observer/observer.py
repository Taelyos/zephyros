"""Listens on TCP and UDP ports and prints one line for every peer that reaches it.

Usage: observer.py tcp:8080 udp:8080 ...

A test proves from the listener's side that something did or did not arrive. On TCP it answers
"hello from <name>", except that `GET /redirect` is answered with a redirect to the address in
the environment variable REDIRECT_TO (the stand-in of a public page that redirects to a
protected address, ATK-42).
"""

import os
import socket
import sys
import threading

NAME = os.environ.get("OBS_NAME", "observer")
REDIRECT_TO = os.environ.get("REDIRECT_TO", "http://10.77.0.5/")


def answer(conn: socket.socket) -> bytes:
    conn.settimeout(0.5)
    try:
        first = conn.recv(1024)
    except OSError:
        first = b""
    if first.startswith(b"GET /redirect") or first.startswith(b"POST "):
        return (
            f"HTTP/1.1 302 Found\r\nLocation: {REDIRECT_TO}\r\nContent-Length: 0\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
    if first.startswith(b"GET "):
        body = f"hello from {NAME}\n"
        return (
            f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
        ).encode()
    return f"hello from {NAME}\n".encode()


def serve_tcp(port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(64)
    while True:
        conn, peer = server.accept()
        local = conn.getsockname()
        print(f"TCP {peer[0]}:{peer[1]} -> {local[0]}:{local[1]}", flush=True)
        threading.Thread(target=reply, args=(conn,), daemon=True).start()


def reply(conn: socket.socket) -> None:
    try:
        conn.sendall(answer(conn))
    except OSError:
        pass
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
