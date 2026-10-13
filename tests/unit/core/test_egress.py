"""The egress of the hub: the configured hosts, and no other (EXG-48, ATK-83)."""

from __future__ import annotations

import pytest

from zephyros.core.egress import Egress, EgressPolicy, EgressRefusal, host_of


def test_only_the_configured_hosts_are_allowed() -> None:
    policy = EgressPolicy(["github.com", "Provider.Example.", " notify.example "])
    assert policy.hosts() == ["github.com", "notify.example", "provider.example"]
    assert policy.allows("GITHUB.COM")
    assert policy.allows("provider.example.")
    assert not policy.allows("elsewhere.example")
    assert policy.allows_url("https://github.com/Taelyos/zephyros.git")
    assert not policy.allows_url("http://elsewhere.example/hook")


def test_a_refused_destination_raises_with_host_and_purpose() -> None:
    policy = EgressPolicy(["github.com"])
    with pytest.raises(EgressRefusal) as refusal:
        policy.check("https://elsewhere.example/hook", purpose="notification")
    assert refusal.value.egress == Egress(
        "elsewhere.example", "notification", "not a configured host"
    )
    with pytest.raises(EgressRefusal):
        policy.check("", purpose="notification")


def test_host_of_reads_the_authority_only() -> None:
    assert host_of("https://user@github.com:443/x.git") == "github.com"
    assert host_of("http://elsewhere.example:8091/steal") == "elsewhere.example"
    assert host_of("not a url") == ""
