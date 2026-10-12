"""JWE interoperability, confidentiality and fail-closed claim validation."""

import json
import time

import pytest
from jwcrypto import jwe, jwk
from pydantic import ValidationError

from mcp_gateway import InvalidToken, TokenCodec, UpstreamCredentials, decode_key, generate_key


def test_standard_jwe_round_trip_and_secret_redaction():
    key = generate_key()
    codec = TokenCodec({"primary": decode_key(key)}, active_key_id="primary")
    grants = {
        "bearer": UpstreamCredentials.model_validate({"bearer": "bearer-secret"}),
        "basic": UpstreamCredentials.model_validate(
            {
                "basic": {"username": "alice", "password": "password-secret"},
            }
        ),
        "headers": UpstreamCredentials.model_validate(
            {
                "headers": {"X-API-Key": "header-secret"},
                "custom": {"refresh": "refresh-secret"},
            }
        ),
        "stdio": UpstreamCredentials.model_validate({"env": {"API_KEY": "env-secret"}}),
        "public": UpstreamCredentials(),
    }
    token = codec.issue("alice", grants)
    assert len(token.split(".")) == 5
    claims = codec.decode(token)
    assert claims.sub == "alice" and set(claims.upstreams) == set(grants)
    assert claims.upstreams["headers"].headers["x-api-key"].get_secret_value() == "header-secret"
    assert claims.upstreams["headers"].custom == {"refresh": "refresh-secret"}
    redacted = repr(claims) + claims.model_dump_json() + repr(grants)
    for secret in (
        "bearer-secret",
        "password-secret",
        "header-secret",
        "env-secret",
        "refresh-secret",
    ):
        assert secret not in redacted and secret not in token

    reader = jwe.JWE()
    reader.deserialize(token)
    reader.decrypt(jwk.JWK(kty="oct", k=key))
    plaintext = json.loads(reader.payload)
    assert plaintext["upstreams"]["basic"]["basic"]["password"] == "password-secret"
    assert reader.jose_header == {
        "alg": "dir",
        "enc": "A256GCM",
        "typ": "mcp-gateway+jwe",
        "kid": "primary",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "foreign"},
        {"aud": "foreign"},
        {"sub": ""},
        {"jti": ""},
        {"iat": "1000"},
        {"iat": True},
        {"iat": 1001},
        {"nbf": 1001},
        {"exp": 1000},
        {"exp": 999},
        {"exp": 87401},
        {"upstreams": {}},
        {"upstreams": {"invalid.namespace": {}}},
        {"iat": 999, "nbf": 998},
        {"unexpected": "field"},
    ],
)
def test_rejects_authenticated_but_invalid_claims(monkeypatch, change):
    monkeypatch.setattr("mcp_gateway.tokens.time.time", lambda: 1000)
    encoded_key = generate_key()
    codec = TokenCodec({"primary": decode_key(encoded_key)}, active_key_id="primary")
    payload = {
        "iss": codec.issuer,
        "aud": codec.audience,
        "sub": "alice",
        "jti": "id",
        "iat": 1000,
        "nbf": 1000,
        "exp": 1100,
        "upstreams": {"public": {}},
    } | change
    envelope = jwe.JWE(
        json.dumps(payload).encode(),
        protected=json.dumps(
            {
                "alg": "dir",
                "enc": "A256GCM",
                "typ": "mcp-gateway+jwe",
                "kid": "primary",
            }
        ),
    )
    envelope.add_recipient(jwk.JWK(kty="oct", k=encoded_key))
    with pytest.raises(InvalidToken, match="Invalid or expired gateway token"):
        codec.decode(envelope.serialize(compact=True))


@pytest.mark.parametrize(
    "header_change",
    [
        {"alg": "A256KW", "enc": "A128GCM"},
        {"alg": "A256KW"},
        {"typ": "other"},
        {"kid": "unknown"},
        {"zip": "DEF"},
        {"jku": "http://untrusted.example/keys"},
    ],
)
def test_rejects_unapproved_jwe_headers(header_change):
    encoded_key = generate_key()
    key = jwk.JWK(kty="oct", k=encoded_key)
    codec = TokenCodec({"primary": decode_key(encoded_key)}, active_key_id="primary")
    token = codec.issue("alice", {"public": UpstreamCredentials()})
    reader = jwe.JWE()
    reader.deserialize(token, key=key)
    header = reader.jose_header
    header.update(header_change)
    envelope = jwe.JWE(reader.payload, protected=json.dumps(header))
    envelope.add_recipient(key)
    disallowed = envelope.serialize(compact=True)
    # These envelopes are cryptographically valid; rejection is a gateway policy.
    reader.deserialize(disallowed, key=key)
    with pytest.raises(InvalidToken):
        codec.decode(disallowed)


def test_expiry_tampering_and_foreign_key(codec, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr("mcp_gateway.tokens.time.time", lambda: now)
    token = codec.issue("alice", {"public": UpstreamCredentials()}, ttl_seconds=1)
    parts = token.split(".")
    parts[3] = ("A" if parts[3][0] != "A" else "B") + parts[3][1:]
    with pytest.raises(InvalidToken):
        codec.decode(".".join(parts))
    foreign = TokenCodec({"primary": decode_key(generate_key())}, active_key_id="primary")
    with pytest.raises(InvalidToken):
        foreign.decode(token)
    assert codec.decode(token).sub == "alice"
    monkeypatch.setattr("mcp_gateway.tokens.time.time", lambda: now + 1)
    with pytest.raises(InvalidToken):
        codec.decode(token)


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c.d.e", "x" * 8193, "{}", "☃.x.x.x.x"])
def test_malformed_tokens_have_one_secret_free_error(codec, token):
    with pytest.raises(InvalidToken, match="^Invalid or expired gateway token$"):
        codec.decode(token)


def test_key_rotation_and_retirement():
    old, new = decode_key(generate_key()), decode_key(generate_key())
    first = TokenCodec({"old": old}, active_key_id="old")
    rotated = TokenCodec({"old": old, "new": new}, active_key_id="new")
    retired = TokenCodec({"new": new}, active_key_id="new")
    old_token = first.issue("alice", {"public": UpstreamCredentials()})
    new_token = rotated.issue("bob", {"public": UpstreamCredentials()})
    assert rotated.decode(old_token).sub == "alice"
    assert rotated.decode(new_token).sub == "bob"
    assert retired.decode(new_token).sub == "bob"
    with pytest.raises(InvalidToken):
        retired.decode(old_token)


@pytest.mark.parametrize("ttl", [0, -1, 86401, True])
def test_issuance_rejects_invalid_lifetimes(codec, ttl):
    with pytest.raises(ValueError):
        codec.issue("alice", {"public": UpstreamCredentials()}, ttl_seconds=ttl)


def test_issuance_rejects_empty_and_oversized_grants(codec):
    with pytest.raises(ValidationError):
        codec.issue("alice", {})
    with pytest.raises(ValueError, match="8192"):
        codec.issue("alice", {"huge": UpstreamCredentials.model_validate({"bearer": "s" * 8192})})


@pytest.mark.parametrize("key", ["password", "!" * 43, "", generate_key() + "X"])
def test_key_loader_rejects_non_keys(key):
    with pytest.raises(ValueError, match="32 random bytes"):
        decode_key(key)
