"""Tokens: issue, authenticate, revoke, rotate (EXG-42, EXG-57, EXG-60; ADR 0013).

A token is 256 random bits with a visible kind prefix. The hub keeps only its SHA-256 and a
record. Authenticating a call means looking the hash up and checking the record: nothing
inside the token is read as a claim, and nothing the caller writes in the request (a side, a
project, a forwarded address) is used. Time comes from the hub's clock.

A project token is bound to the address of the instance it was issued to, and the hub
compares it with the TCP peer address of the connection. It refuses on any doubt.

The operator and main-agent tokens are bound to no address; they expire after the lifetime of
the configuration, and are rotated on request (ADR 0026).
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import ipaddress
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from zephyros.core.audit import Reason
from zephyros.core.clock import Clock
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import MAIN_AGENT, OPERATOR, Principal

_SECRET = re.compile(r"^zph_[hmoc]_[A-Za-z0-9_-]{43}$")


class TokenKind(StrEnum):
    OPERATOR = "operator"
    MAIN_AGENT = "main-agent"
    PROJECT = "project"


class Endpoint(StrEnum):
    """The listener a call arrived on. A token is accepted on one kind of endpoint only."""

    AGENT = "agent"
    MAIN_AGENT = "main-agent"
    OPERATOR = "operator"


_PREFIX = {
    (TokenKind.OPERATOR, None): "zph_h_",
    (TokenKind.MAIN_AGENT, None): "zph_m_",
    (TokenKind.PROJECT, Side.OWNER): "zph_o_",
    (TokenKind.PROJECT, Side.CONTRACTOR): "zph_c_",
}
_ENDPOINT_OF_KIND = {
    TokenKind.OPERATOR: Endpoint.OPERATOR,
    TokenKind.MAIN_AGENT: Endpoint.MAIN_AGENT,
    TokenKind.PROJECT: Endpoint.AGENT,
}


@dataclass(frozen=True, slots=True)
class TokenRecord:
    token_id: str
    token_hash: str
    kind: TokenKind
    principal: Principal
    instance_id: str | None
    bound_address: str | None
    issued_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind is TokenKind.PROJECT:
            if (
                not self.principal.is_instance
                or self.instance_id is None
                or self.bound_address is None
            ):
                raise ValueError(
                    "a project token belongs to an instance and is bound to an address"
                )
        elif self.instance_id is not None or self.bound_address is not None:
            raise ValueError("only a project token is bound to an instance")


class TokenStore(Protocol):
    def add(self, record: TokenRecord) -> None: ...

    def get_by_hash(self, token_hash: str) -> TokenRecord | None: ...

    def get(self, token_id: str) -> TokenRecord | None: ...

    def revoke(self, token_id: str, at: datetime) -> bool:
        """Mark a token revoked. False when it is unknown or already revoked."""
        ...

    def for_instance(self, instance_id: str) -> list[TokenRecord]: ...

    def of_kind(self, kind: TokenKind) -> list[TokenRecord]: ...

    def set_expiry(self, token_id: str, at: datetime) -> bool:
        """Give a token that has no expiry one. False when it is unknown or already has one."""
        ...


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """What issuing returns. `secret` is shown once: the hub keeps only its hash."""

    token_id: str
    secret: str = field(repr=False)
    record: TokenRecord


@dataclass(frozen=True, slots=True)
class Authenticated:
    principal: Principal
    record: TokenRecord


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: Reason
    token_id: str | None
    """The identifier of the record when one was found; never the token."""
    presented: str
    """The kind prefix the caller presented, or 'malformed'. Safe to log."""
    principal: Principal | None = None
    """Whose token it is, when the hub found the record: the log names that principal."""
    expires_at: datetime | None = None
    """When the token expired, for a refusal of an expired token."""


AuthResult = Authenticated | Rejected


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_address(text: str | None) -> str | None:
    """A canonical form of an IP address, or None. IPv4-mapped IPv6 reads as IPv4."""
    if not text:
        return None
    try:
        address = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _same_address(first: str | None, second: str | None) -> bool:
    left, right = normalize_address(first), normalize_address(second)
    if left is None or right is None:
        return False
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


class TokenService:
    def __init__(
        self,
        store: TokenStore,
        clock: Clock,
        *,
        new_secret: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_secret = new_secret or (lambda: secrets.token_urlsafe(32))
        self._last_seen: dict[str, datetime] = {}

    # ------------------------------------------------------------------ issue
    def _issue(
        self,
        kind: TokenKind,
        principal: Principal,
        side: Side | None,
        *,
        instance_id: str | None,
        bound_address: str | None,
        lifetime_seconds: int | None,
    ) -> IssuedToken:
        body = self._new_secret()
        secret = _PREFIX[(kind, side)] + body
        if not _SECRET.fullmatch(secret):
            raise ValueError("the secret generator produced a malformed token")
        now = self._clock.now()
        expires = None if lifetime_seconds is None else now + timedelta(seconds=lifetime_seconds)
        record = TokenRecord(
            token_id="t_" + secrets.token_hex(6),
            token_hash=hash_secret(secret),
            kind=kind,
            principal=principal,
            instance_id=instance_id,
            bound_address=bound_address,
            issued_at=now,
            expires_at=expires,
        )
        self._store.add(record)
        return IssuedToken(record.token_id, secret, record)

    def issue_project_token(
        self,
        principal: Principal,
        instance_id: str,
        bound_address: str,
        lifetime_seconds: int,
    ) -> IssuedToken:
        """A token for an instance, bound to the address of its guard."""
        if not principal.is_instance or principal.side is None:
            raise ValueError("a project token belongs to an instance")
        address = normalize_address(bound_address)
        if address is None:
            raise ValueError("a project token is bound to an IP address")
        if lifetime_seconds <= 0:
            raise ValueError("a token has a positive lifetime")
        return self._issue(
            TokenKind.PROJECT,
            principal,
            principal.side,
            instance_id=instance_id,
            bound_address=address,
            lifetime_seconds=lifetime_seconds,
        )

    def issue_operator_token(self, lifetime_seconds: int | None = None) -> IssuedToken:
        return self._issue(
            TokenKind.OPERATOR,
            OPERATOR,
            None,
            instance_id=None,
            bound_address=None,
            lifetime_seconds=lifetime_seconds,
        )

    def issue_main_agent_token(self, lifetime_seconds: int | None = None) -> IssuedToken:
        return self._issue(
            TokenKind.MAIN_AGENT,
            MAIN_AGENT,
            None,
            instance_id=None,
            bound_address=None,
            lifetime_seconds=lifetime_seconds,
        )

    # ------------------------------------------------------------------ authenticate
    def authenticate(
        self, secret: str | None, *, peer_address: str | None, endpoint: Endpoint
    ) -> AuthResult:
        """Identify the caller from the token and the connection. Nothing else is read."""
        if not secret or not _SECRET.fullmatch(secret):
            return Rejected(Reason.UNKNOWN_TOKEN, None, "malformed")
        presented = secret[:6]
        record = self._store.get_by_hash(hash_secret(secret))
        if record is None or not hmac.compare_digest(record.token_hash, hash_secret(secret)):
            return Rejected(Reason.UNKNOWN_TOKEN, None, presented)
        if record.revoked_at is not None:
            return Rejected(Reason.REVOKED, record.token_id, presented, record.principal)
        now = self._clock.now()
        if record.expires_at is not None and now >= record.expires_at:
            return Rejected(
                Reason.EXPIRED, record.token_id, presented, record.principal, record.expires_at
            )
        if _ENDPOINT_OF_KIND[record.kind] is not endpoint:
            return Rejected(Reason.WRONG_ENDPOINT, record.token_id, presented, record.principal)
        if record.kind is TokenKind.PROJECT and not _same_address(
            record.bound_address, peer_address
        ):
            return Rejected(Reason.WRONG_SOURCE, record.token_id, presented, record.principal)
        if record.kind is TokenKind.PROJECT:
            self._last_seen[record.token_id] = now
        return Authenticated(record.principal, record)

    def last_seen(self, token_id: str) -> datetime | None:
        """When the token last authenticated a call, in this run of the hub. Not stored: it only
        tells the watchdog that an instance is still talking to the hub."""
        return self._last_seen.get(token_id)

    # ------------------------------------------------------------------ revoke, rotate
    def revoke(self, token_id: str) -> bool:
        self._last_seen.pop(token_id, None)
        return self._store.revoke(token_id, self._clock.now())

    def revoke_instance(self, instance_id: str) -> int:
        """Revoke every live token of an instance, as when it stops (EXG-42)."""
        count = 0
        for record in self._store.for_instance(instance_id):
            self._last_seen.pop(record.token_id, None)
            if record.revoked_at is None and self._store.revoke(record.token_id, self._clock.now()):
                count += 1
        return count

    def needs_rotation(self, record: TokenRecord, rotate_before_seconds: int) -> bool:
        """True when less than `rotate_before_seconds` of the lifetime remains."""
        if record.expires_at is None:
            return False
        return self._clock.now() >= record.expires_at - timedelta(seconds=rotate_before_seconds)

    def _live(self, record: TokenRecord) -> bool:
        return record.revoked_at is None and (
            record.expires_at is None or self._clock.now() < record.expires_at
        )

    def live_of_kind(self, kind: TokenKind) -> list[TokenRecord]:
        """The tokens of a kind that are neither revoked nor expired, oldest first."""
        live = [record for record in self._store.of_kind(kind) if self._live(record)]
        return sorted(live, key=lambda record: record.issued_at)

    def give_lifetime(self, kind: TokenKind, lifetime_seconds: int) -> list[TokenRecord]:
        """Give each live token of `kind` that never expires the lifetime, counted from now: a
        token issued before its kind expired keeps working that long, no longer (ADR 0026)."""
        if lifetime_seconds <= 0:
            raise ValueError("a token has a positive lifetime")
        expires = self._clock.now() + timedelta(seconds=lifetime_seconds)
        return [
            dataclasses.replace(record, expires_at=expires)
            for record in self.live_of_kind(kind)
            if record.expires_at is None and self._store.set_expiry(record.token_id, expires)
        ]

    def live_project_tokens(self, instance_id: str) -> list[TokenRecord]:
        """The tokens of the instance that are neither revoked nor expired, newest first."""
        now = self._clock.now()
        live = [
            record
            for record in self._store.for_instance(instance_id)
            if record.revoked_at is None and (record.expires_at is None or now < record.expires_at)
        ]
        return sorted(live, key=lambda record: record.issued_at, reverse=True)

    def live_project_token(self, instance_id: str) -> TokenRecord | None:
        """The newest token of the instance that is neither revoked nor expired."""
        live = self.live_project_tokens(instance_id)
        return live[0] if live else None
