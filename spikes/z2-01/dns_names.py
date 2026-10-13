"""Host-side decoder for spike Z2-01: the DNS names asked for in a packet capture.

    python3 dns_names.py capture.pcap

Reads a classic pcap file written by tcpdump on the loopback of an instance (the runtime's
embedded resolver is reached there) and prints each distinct "name type" of a DNS question,
one per line. Standard library only.
"""

from __future__ import annotations

import struct
import sys

LINK_HEADER = {1: 14, 113: 16, 276: 20}  # Ethernet, Linux cooked v1, Linux cooked v2
QTYPES = {1: "A", 28: "AAAA", 5: "CNAME", 15: "MX", 16: "TXT", 12: "PTR", 65: "HTTPS"}


def read_name(payload: bytes, offset: int) -> str | None:
    labels = []
    while offset < len(payload):
        length = payload[offset]
        if length == 0:
            return ".".join(labels)
        if length & 0xC0 or offset + 1 + length > len(payload):
            return None
        labels.append(payload[offset + 1 : offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length
    return None


def dns_questions(udp_payload: bytes) -> list[str]:
    if len(udp_payload) < 12:
        return []
    flags, questions = struct.unpack("!2xHH", udp_payload[:6])
    if flags & 0x8000 or questions < 1:  # a response, or no question
        return []
    name = read_name(udp_payload, 12)
    if name is None:
        return []
    end = 12 + len(name) + 2
    qtype = struct.unpack("!H", udp_payload[end : end + 2])[0] if end + 2 <= len(udp_payload) else 0
    return [f"{name} {QTYPES.get(qtype, qtype)}"]


def main() -> int:
    with open(sys.argv[1], "rb") as handle:
        data = handle.read()
    magic = struct.unpack("<I", data[:4])[0]
    endian = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
    link_type = struct.unpack(endian + "I", data[20:24])[0]
    header = LINK_HEADER.get(link_type)
    if header is None:
        print(f"unsupported link type {link_type}", file=sys.stderr)
        return 2
    found: set[str] = set()
    offset = 24
    while offset + 16 <= len(data):
        captured = struct.unpack(endian + "I", data[offset + 8 : offset + 12])[0]
        packet = data[offset + 16 : offset + 16 + captured]
        offset += 16 + captured
        ip = packet[header:]
        if len(ip) >= 28 and ip[0] >> 4 == 4 and ip[9] == 17:  # IPv4, UDP
            ihl = (ip[0] & 0x0F) * 4
            found.update(dns_questions(ip[ihl + 8 :]))
        elif len(ip) >= 48 and ip[0] >> 4 == 6 and ip[6] == 17:  # IPv6, UDP
            found.update(dns_questions(ip[48:]))
    for entry in sorted(found):
        print(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
