"""Trusted identities and per-association capabilities."""

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from kapy.rpc import RpcError
from kapy.settings import Settings


class Rejected(RpcError):
    """A definite business rejection, distinguished from transport errors by its source."""


def denied(message: str = "Permission denied") -> Rejected:
    return Rejected(-32001, message, {"kind": "unauthorized", "retryable": False})


@dataclass(frozen=True, slots=True)
class Principal:
    kind: Literal["operator", "session", "frontend"]
    machine_id: str | None = None
    session_id: UUID | None = None
    frontend_id: str | None = None
    subject: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "frontend" and (
            not self.frontend_id
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.frontend_id)
            or self.frontend_id in {"operator", "session"}
            or not self.subject
            or len(self.subject) > 1024
            or any(ord(c) < 32 for c in self.subject)
        ):
            raise ValueError("Frontend identity requires a valid namespace and subject")

    @property
    def id(self) -> str:
        if self.kind == "operator":
            return "operator"
        if self.kind == "session" and self.session_id is not None:
            return f"session:{self.session_id}"
        if self.kind == "frontend":
            return f"{self.frontend_id}:{self.subject}"
        raise denied("Incomplete identity")

    @classmethod
    def from_id(cls, identity: str) -> Principal:
        """Decode a previously authenticated durable identity, never a user-supplied token."""
        if identity == "operator":
            return cls("operator")
        namespace, separator, subject = identity.partition(":")
        if not separator:
            raise ValueError("Invalid persisted principal")
        if namespace == "session":
            return cls("session", session_id=UUID(subject))
        return cls("frontend", frontend_id=namespace, subject=subject)


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
