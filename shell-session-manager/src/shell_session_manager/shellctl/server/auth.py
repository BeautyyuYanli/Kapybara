"""Shared bearer-token verification for shellctl transports.

HTTP headers and gRPC metadata should enforce the same opt-in bearer auth
contract so transport additions do not accidentally drift from the existing API
behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.errors import ShellctlServerError


@dataclass(slots=True, frozen=True)
class AuthVerifier:
    """Verify the optional bearer token configured for shellctl.

    When `token` is `None`, auth is disabled and requests are accepted without an
    `Authorization` header. Callers intentionally skip this verifier for public
    health probes.
    """

    token: str | None

    @classmethod
    def from_config(cls, config: ShellctlConfig) -> AuthVerifier:
        """Build a verifier from the normalized server configuration."""

        return cls(token=config.auth_token)

    def verify_authorization_header(self, authorization: str | None) -> None:
        """Validate a bearer token header or metadata value.

        Raises:
            ShellctlServerError: If auth is enabled and the header is missing or
                does not exactly match `Bearer <token>`.
        """

        if self.token is None:
            return
        if authorization != f"Bearer {self.token}":
            raise ShellctlServerError(
                401, "unauthorized", "Missing or invalid bearer token"
            )


__all__ = ["AuthVerifier"]
