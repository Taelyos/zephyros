"""The operator and main-agent tokens: issued at the first start, with a lifetime, rotated on
request (ADR 0026).

Each token has a file, mode 0600, that the human reads (`zephyros hub call` reads the operator's).
The hub keeps the hashes. When it starts, a token that never expired (one issued before the
lifetime existed) gets the configured lifetime, counted from that start; a file without a working
token gets a new one. A rotation issues a new token, writes it to the file, then revokes every
other live token of its kind. The rotation is asked on the control socket, a Unix socket open to
the user of the hub alone: it works when the operator token has expired, and asks for nothing
the file system of the hub does not already grant. Issuing, giving a lifetime and revoking are
logged; so is every refusal of an expired token, by the authentication itself.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from zephyros.adapters.http.common import BodyError, read_json
from zephyros.core.audit import HUB_STREAM, SYSTEM_PRINCIPAL, Action, AuditEvent, AuditLog, Outcome
from zephyros.core.audit import Reason as AuditReason
from zephyros.core.clock import format_time
from zephyros.core.config import Config
from zephyros.core.principals import OPERATOR
from zephyros.core.tokens import (
    Authenticated,
    Endpoint,
    IssuedToken,
    TokenKind,
    TokenRecord,
    TokenService,
)

KINDS: Final = {"operator": TokenKind.OPERATOR, "main-agent": TokenKind.MAIN_AGENT}
"""The kinds a rotation names, as the command line spells them."""
_ENDPOINT: Final = {
    TokenKind.OPERATOR: Endpoint.OPERATOR,
    TokenKind.MAIN_AGENT: Endpoint.MAIN_AGENT,
}
SECONDS_PER_DAY: Final = 86_400


def write_token_file(path: Path, secret: str) -> None:
    """Replace `path` with a file holding `secret`, mode 0600 from its creation: a reader sees
    the old token or the new one, never a part of either."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(secret + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class Rotation:
    kind: str
    token_id: str
    expires_at: str
    revoked: tuple[str, ...]
    file: str


@dataclass
class ChannelTokens:
    tokens: TokenService
    audit: AuditLog
    files: Mapping[TokenKind, Path]
    lifetime_seconds: int
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_config(cls, config: Config, tokens: TokenService, audit: AuditLog) -> ChannelTokens:
        return cls(
            tokens,
            audit,
            {
                TokenKind.OPERATOR: Path(config.paths.operator_token_file),
                TokenKind.MAIN_AGENT: Path(config.paths.main_agent_token_file),
            },
            config.tokens.channel_lifetime_days * SECONDS_PER_DAY,
        )

    # ------------------------------------------------------------------ at start
    def prepare(self) -> None:
        """Give a lifetime to the tokens that had none, and a working token to each file."""
        with self._lock:
            for kind, path in self.files.items():
                for record in self.tokens.give_lifetime(kind, self.lifetime_seconds):
                    self._log(
                        Action.TOKEN_LIFETIME,
                        kind,
                        record,
                        why="issued before the operator and main-agent tokens expired",
                    )
                why = self._why_not_working(kind, path)
                if why is not None:
                    issued = self._issue(kind)
                    write_token_file(path, issued.secret)
                    self._log(Action.TOKEN_ISSUE, kind, issued.record, why=why)

    def _why_not_working(self, kind: TokenKind, path: Path) -> str | None:
        """None when the file holds a working token; otherwise why a new one is issued."""
        if not path.exists():
            return "no token file"
        current = path.read_text(encoding="ascii").strip()
        found = self.tokens.authenticate(current, peer_address=None, endpoint=_ENDPOINT[kind])
        if isinstance(found, Authenticated):
            return None
        return f"the token of the file is refused: {found.reason.value}"

    # ------------------------------------------------------------------ on request
    def rotate(self, kind: TokenKind) -> Rotation:
        """A new token in the file, then every other live token of the kind revoked. When the
        file cannot be written, the new token is revoked and the old ones stay."""
        with self._lock:
            issued = self._issue(kind)
            path = self.files[kind]
            try:
                write_token_file(path, issued.secret)
            except OSError:
                self.tokens.revoke(issued.token_id)
                raise
            self._log(Action.TOKEN_ISSUE, kind, issued.record, why="rotation")
            revoked: list[str] = []
            for record in self.tokens.live_of_kind(kind):
                if record.token_id != issued.token_id and self.tokens.revoke(record.token_id):
                    revoked.append(record.token_id)
                    self._log(Action.TOKEN_REVOKE, kind, record, why="rotation")
            expires = issued.record.expires_at
            return Rotation(
                kind.value,
                issued.token_id,
                format_time(expires) if expires else "",
                tuple(revoked),
                str(path),
            )

    def _issue(self, kind: TokenKind) -> IssuedToken:
        if kind is TokenKind.OPERATOR:
            return self.tokens.issue_operator_token(self.lifetime_seconds)
        if kind is TokenKind.MAIN_AGENT:
            return self.tokens.issue_main_agent_token(self.lifetime_seconds)
        raise ValueError(f"{kind.value} is not a token of a channel of the machine")

    def _log(self, action: Action, kind: TokenKind, record: TokenRecord, *, why: str) -> None:
        principal = OPERATOR.id if why == "rotation" else SYSTEM_PRINCIPAL
        self.audit.append(
            AuditEvent(
                HUB_STREAM,
                principal,
                action,
                Outcome.OK,
                AuditReason.NONE,
                {
                    "kind": kind.value,
                    "token_id": record.token_id,
                    "expires_at": format_time(record.expires_at) if record.expires_at else None,
                    "why": why,
                },
            )
        )


def build_control_app(channel: ChannelTokens) -> Starlette:
    """The control socket: one request, a rotation. Reaching the socket is the authentication:
    it is open to the user of the hub alone, the owner of the token files."""

    async def rotate(request: Request) -> Response:
        try:
            body = await read_json(request)
        except BodyError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=error.status)
        name = body.get("kind") if isinstance(body, dict) else None
        if not isinstance(name, str) or name not in KINDS:
            return JSONResponse(
                {"ok": False, "error": f"kind is one of: {', '.join(KINDS)}"}, status_code=400
            )
        try:
            done = channel.rotate(KINDS[name])
        except OSError as error:
            return JSONResponse(
                {"ok": False, "error": f"the token file cannot be written: {error.strerror}"},
                status_code=500,
            )
        return JSONResponse(
            {
                "ok": True,
                "kind": name,
                "token_id": done.token_id,
                "expires_at": done.expires_at,
                "revoked": list(done.revoked),
                "file": done.file,
            }
        )

    return Starlette(routes=[Route("/tokens/rotate", rotate, methods=["POST"])])
