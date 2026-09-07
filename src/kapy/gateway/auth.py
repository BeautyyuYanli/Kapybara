"""Trusted identities and per-association capabilities."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from kapy.rpc import RpcError
from kapy.settings import Settings


def denied(message: str = "Permission denied") -> RpcError:
    return RpcError(-32001, message, {"kind": "unauthorized", "retryable": False})


@dataclass(frozen=True, slots=True)
class Principal:
    kind: Literal["operator", "session", "telegram"]
    machine_id: str | None = None
    session_id: UUID | None = None
    telegram_route: tuple[int, int, int] | None = None

    @property
    def id(self) -> str:
        if self.kind == "operator":
            return "operator"
        if self.kind == "session" and self.session_id is not None:
            return f"session:{self.session_id}"
        if self.kind == "telegram" and self.telegram_route is not None:
            return "telegram:" + ":".join(map(str, self.telegram_route))
        raise denied("Incomplete identity")


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def operator(self, token: str) -> Principal:
        secret = self.settings.control_token
        if not secret or not token or not hmac.compare_digest(token, secret.get_secret_value()):
            raise denied()
        return Principal("operator")

    def machine(self, machine_id: str, token: str) -> None:
        secret = self.settings.machine_tokens.get(machine_id)
        if not secret or not token or not hmac.compare_digest(token, secret.get_secret_value()):
            raise denied()

    def token(self, session_id: UUID, machine_id: str) -> str:
        secret = self.settings.session_signing_key
        if secret is None:
            raise RuntimeError("Session signing key is not configured")
        message = f"kapy.session.v1\0{session_id}\0{machine_id}".encode()
        digest = hmac.new(secret.get_secret_value().encode(), message, hashlib.sha256).hexdigest()
        return f"v1.{session_id}.{machine_id}.{digest}"

    def session(self, session_id: UUID, machine_id: str, token: str) -> Principal:
        expected = self.token(session_id, machine_id)
        if not token or not hmac.compare_digest(expected, token):
            raise denied()
        return Principal("session", machine_id=machine_id, session_id=session_id)


def bearer(header: str | None) -> str:
    if header is None:
        raise denied("Bearer authentication required")
    scheme, separator, value = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not value or " " in value:
        raise denied("Bearer authentication required")
    return value
