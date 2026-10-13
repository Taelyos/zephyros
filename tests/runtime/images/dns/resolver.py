"""A tiny scripted DNS server for the runtime bench (decision 38).

Answers the variants of ATK-41 deterministically, without touching the Internet:

- private.test   -> A 10.77.0.5 (a protected address, always)
- rebind.test    -> A alternates public then protected, TTL 0 (a rebinding attack)
- cname.test     -> CNAME mid.test, mid.test -> A 10.77.0.5 (a chain into the private range)
- mixed.test     -> A 93.184.216.34 and AAAA fd00::5 (public v4, protected v6)
- public.test    -> A 93.184.216.34 (the positive control)

Usage: resolver.py [port] (53 by default). UDP only; one question per query.
"""

import socket
import struct
import sys

PROTECTED = "10.77.0.5"
PUBLIC = "93.184.216.34"
PROTECTED_V6 = "fd00::5"

Record = tuple[str, str, int, "str | None"]
ANSWERS: dict[str, list[Record]] = {
    "private.test": [("A", PROTECTED, 30, None)],
    "cname.test": [("CNAME", "mid.test", 30, None), ("A", PROTECTED, 30, "mid.test")],
    "mid.test": [("A", PROTECTED, 30, None)],
    "mixed.test": [("A", PUBLIC, 30, None), ("AAAA", PROTECTED_V6, 30, None)],
    "public.test": [("A", PUBLIC, 30, None)],
}
_rebind_count = 0


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def parse_question(data: bytes) -> tuple[str, int, int]:
    """Return (qname, qtype, end_offset) of the first question."""
    offset = 12
    labels = []
    while data[offset] != 0:
        length = data[offset]
        labels.append(data[offset + 1 : offset + 1 + length].decode("ascii"))
        offset += 1 + length
    offset += 1
    qtype, _qclass = struct.unpack("!HH", data[offset : offset + 4])
    return ".".join(labels), qtype, offset + 4


def answers_for(name: str, qtype: int) -> list[Record]:
    global _rebind_count
    if name == "rebind.test":
        if qtype != 1:  # only the A queries count: one per resolution, deterministic
            return []
        _rebind_count += 1
        address = PUBLIC if _rebind_count % 2 == 1 else PROTECTED
        return [("A", address, 0, None)]
    return ANSWERS.get(name, [])


def build_response(data: bytes, qname: str, qtype: int, qend: int) -> bytes:
    question = data[12:qend]
    answer = answers_for(qname, qtype)
    records = b""
    count = 0
    for kind, value, ttl, owner in answer:
        owner = owner or qname
        if kind == "CNAME":
            records += (
                encode_name(owner)
                + struct.pack("!HHIH", 5, 1, ttl, len(encode_name(value)))
                + encode_name(value)
            )
        elif kind == "A":
            records += encode_name(owner) + struct.pack("!HHIH", 1, 1, ttl, 4)
            records += socket.inet_aton(value)
        elif kind == "AAAA":
            records += encode_name(owner) + struct.pack("!HHIH", 28, 1, ttl, 16)
            records += socket.inet_pton(socket.AF_INET6, value)
        count += 1
    flags = 0x8180 if count else 0x8183  # standard response, NXDOMAIN when nothing is served
    header = struct.pack("!HHHHHH", struct.unpack("!H", data[:2])[0], flags, 1, count, 0, 0)
    return header + question + records


def serve(port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("0.0.0.0", port))
    print(f"scripted resolver on udp:{port}", flush=True)
    while True:
        data, peer = server.recvfrom(512)
        try:
            qname, qtype, qend = parse_question(data)
        except (IndexError, struct.error, UnicodeDecodeError):
            continue
        print(f"query {qname} type {qtype} from {peer[0]}", flush=True)
        server.sendto(build_response(data, qname, qtype, qend), peer)


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 53)
