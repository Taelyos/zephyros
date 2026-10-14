"""The decisions of the egress proxy of the hub (ADR 0024), without a network."""

from __future__ import annotations

import pytest

from zephyros.core.config import DEFAULT_ALLOWED_HOSTS
from zephyros.core.instance_egress import (
    Destination,
    EgressLevel,
    InstanceEgressPolicy,
    Refused,
    is_protected,
    parse_connect,
)

REGULATED = InstanceEgressPolicy(EgressLevel.REGULATED, DEFAULT_ALLOWED_HOSTS)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("CONNECT pypi.org:443 HTTP/1.1", Destination("pypi.org", 443)),
        ("CONNECT example.org:8443 HTTP/1.0", Destination("example.org", 8443)),
        ("CONNECT 93.184.216.34:443 HTTP/1.1", Destination("93.184.216.34", 443)),
    ],
)
def test_a_connect_request_names_its_destination(line: str, expected: Destination) -> None:
    assert parse_connect(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "GET http://pypi.org/simple/ HTTP/1.1",  # a plain proxied request: no tunnel, refused
        "CONNECT pypi.org HTTP/1.1",
        "CONNECT pypi.org:0 HTTP/1.1",
        "CONNECT pypi.org:70000 HTTP/1.1",
        "CONNECT user@pypi.org:443 HTTP/1.1",
        "CONNECT [::1]:443 HTTP/1.1",
        "CONNECT pypi.org:443 HTTP/2",
        "CONNECT  pypi.org:443 HTTP/1.1",
        "connect pypi.org:443 HTTP/1.1",
        "CONNECT pypi.org:443",
        "",
    ],
)
def test_anything_but_a_plain_connect_is_not_a_destination(line: str) -> None:
    assert parse_connect(line) is None


@pytest.mark.parametrize("host", DEFAULT_ALLOWED_HOSTS)
def test_regulated_lets_the_package_registries_through_on_443(host: str) -> None:
    assert REGULATED.decide(Destination(host, 443)) is None


@pytest.mark.parametrize(
    ("destination", "why"),
    [
        (Destination("example.org", 443), Refused.NOT_ALLOWED),
        (Destination("github.com", 443), Refused.NOT_ALLOWED),  # not in the default list
        (Destination("upload.pypi.org", 443), Refused.NOT_ALLOWED),  # no subdomain by default
        (Destination("PyPI.org", 443), Refused.NOT_ALLOWED),
        (Destination("pypi.org.", 443), Refused.NOT_ALLOWED),
        (Destination("151.101.0.223", 443), Refused.NOT_ALLOWED),  # an address is not a name
        (Destination("pypi.org", 80), Refused.PORT),
        (Destination("pypi.org", 22), Refused.PORT),
    ],
)
def test_regulated_refuses_every_other_destination(destination: Destination, why: Refused) -> None:
    assert REGULATED.decide(destination) is why


@pytest.mark.parametrize("level", [EgressLevel.SEALED, EgressLevel.OPEN])
def test_the_proxy_serves_no_instance_outside_the_regulated_level(level: EgressLevel) -> None:
    policy = InstanceEgressPolicy(level, DEFAULT_ALLOWED_HOSTS)
    assert policy.decide(Destination("pypi.org", 443)) is Refused.NOT_REGULATED


def test_an_operator_adds_a_host_by_naming_it() -> None:
    policy = InstanceEgressPolicy("regulated", (*DEFAULT_ALLOWED_HOSTS, "github.com"))
    assert policy.decide(Destination("github.com", 443)) is None
    assert "github.com" in policy.hosts()


def test_an_entry_that_is_not_a_host_name_never_matches() -> None:
    policy = InstanceEgressPolicy("regulated", ("10.0.0.1", "*.example.org", "pypi.org"))
    assert policy.hosts() == ["pypi.org"]
    assert policy.decide(Destination("10.0.0.1", 443)) is Refused.NOT_ALLOWED


@pytest.mark.parametrize(
    "address",
    [
        "10.77.0.5",
        "172.31.250.5",
        "192.168.7.7",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.7.7",
        "0.0.0.0",  # noqa: S104  # reason: an address under test, not a bind
        "::1",
        "fd00::5",
        "fe80::1",
        "::ffff:10.77.0.5",
        "not an address",
    ],
)
def test_every_protected_address_is_protected(address: str) -> None:
    assert is_protected(address)
    assert REGULATED.check_addresses(["151.101.0.223", address]) is Refused.PROTECTED


def test_a_name_with_public_addresses_only_is_relayed() -> None:
    assert not is_protected("151.101.0.223")
    assert REGULATED.check_addresses(["151.101.0.223", "151.101.64.223"]) is None


def test_a_name_that_does_not_resolve_is_refused() -> None:
    assert REGULATED.check_addresses([]) is Refused.NO_ADDRESS
