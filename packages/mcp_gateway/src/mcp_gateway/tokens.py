"""Bounded compact JWE grants using authenticated encryption (dir/A256GCM).

Keys belong only to the gateway and trusted issuers: possession permits issuing
grants, not just decrypting them. Downstream clients receive only the compact JWE.
The key ring permits rotation without storing tokens or plaintext credentials.
"""

import base64
import json
import secrets
import time
from collections.abc import Mapping

from jwcrypto import jwe, jwk
from jwcrypto.common import JWException
from pydantic import ConfigDict, Field

from .models import Model, NamespaceName, UpstreamCredentials

MAX_TOKEN_BYTES = 8192
_TYPE = "mcp-gateway+jwe"


class InvalidToken(ValueError):
    """A malformed, untrusted or inactive gateway credential."""


class TokenClaims(Model):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    iss: str = Field(min_length=1)
    aud: str = Field(min_length=1)
    sub: str = Field(min_length=1)
    jti: str = Field(min_length=1)
    iat: int
    nbf: int
    exp: int
    upstreams: dict[NamespaceName, UpstreamCredentials] = Field(min_length=1, repr=False)


def generate_key() -> str:
    """Return a base64url-encoded random 256-bit shared secret."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def decode_key(encoded: str) -> bytes:
    """Reject passwords and malformed input instead of deriving an encryption key."""
    try:
        key = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise ValueError("JWE key must be base64url-encoded 32 random bytes") from None
    if len(key) != 32:
        raise ValueError("JWE key must be base64url-encoded 32 random bytes")
    return key


class TokenCodec:
    """Issue and validate grants for one issuer/audience with an explicit key ring."""

    def __init__(
        self,
        keys: Mapping[str, bytes],
        *,
        active_key_id: str,
        issuer: str = "kapy-mcp-gateway",
        audience: str = "kapy-mcp-gateway",
        max_lifetime: int = 86400,
    ) -> None:
        if (
            not keys
            or active_key_id not in keys
            or not issuer
            or not audience
            or type(max_lifetime) is not int
            or max_lifetime <= 0
            or any(not kid or len(key) != 32 for kid, key in keys.items())
        ):
            raise ValueError("Provide 256-bit JWE keys, an active key ID and a positive lifetime")
        self._keys = {
            kid: jwk.JWK(kty="oct", k=base64.urlsafe_b64encode(key).decode().rstrip("="))
            for kid, key in keys.items()
        }
        self.active_key_id = active_key_id
        self.issuer = issuer
        self.audience = audience
        self.max_lifetime = max_lifetime

    def issue(
        self,
        subject: str,
        upstreams: Mapping[str, UpstreamCredentials],
        *,
        ttl_seconds: int = 3600,
    ) -> str:
        """Mint one bearer credential; grant entries also authorize public upstreams."""
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= self.max_lifetime:
            raise ValueError("Token lifetime must be positive and at most max_lifetime")
        now = int(time.time())
        payload = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": subject,
            "jti": secrets.token_hex(16),
            "iat": now,
            "nbf": now,
            "exp": now + ttl_seconds,
            "upstreams": {name: grant.plaintext() for name, grant in upstreams.items()},
        }
        TokenClaims.model_validate(payload)
        envelope = jwe.JWE(
            json.dumps(payload, separators=(",", ":")).encode(),
            protected=json.dumps(
                {
                    "alg": "dir",
                    "enc": "A256GCM",
                    "typ": _TYPE,
                    "kid": self.active_key_id,
                }
            ),
            algs=["dir", "A256GCM"],
        )
        envelope.add_recipient(self._keys[self.active_key_id])
        token = envelope.serialize(compact=True)
        if len(token) > MAX_TOKEN_BYTES:
            raise ValueError("Encrypted grant exceeds the 8192-byte token limit")
        return token

    def decode(self, token: str) -> TokenClaims:
        """Authenticate the envelope and claims; all failures use a secret-free error."""
        try:
            if len(token) > MAX_TOKEN_BYTES or token.count(".") != 4:
                raise ValueError
            envelope = jwe.JWE(algs=["dir", "A256GCM"])
            envelope.deserialize(token)
            header = envelope.jose_header
            if (
                set(header) != {"alg", "enc", "typ", "kid"}
                or header["alg"] != "dir"
                or header["enc"] != "A256GCM"
                or header["typ"] != _TYPE
            ):
                raise ValueError
            envelope.decrypt(self._keys[header["kid"]], max_plaintext=MAX_TOKEN_BYTES)
            claims = TokenClaims.model_validate_json(envelope.payload)
            now = int(time.time())
            if (
                claims.iss != self.issuer
                or claims.aud != self.audience
                or not claims.iat <= claims.nbf <= now < claims.exp
                or claims.exp - claims.iat > self.max_lifetime
            ):
                raise ValueError
            return claims
        except (JWException, ValueError, TypeError, KeyError):
            raise InvalidToken("Invalid or expired gateway token") from None
