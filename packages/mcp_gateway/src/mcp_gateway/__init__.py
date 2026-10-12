"""Standalone MCP aggregation and encrypted upstream authentication."""

from .gateway import AuthFactory, create_gateway, create_stdio_gateway
from .models import (
    INLINE_CONFIG_ENV,
    BasicCredentials,
    GatewayConfig,
    HTTPUpstream,
    InlineGatewayConfig,
    StdioUpstream,
    UpstreamCredentials,
)
from .tokens import InvalidToken, TokenClaims, TokenCodec, decode_key, generate_key

__all__ = [
    "AuthFactory",
    "BasicCredentials",
    "GatewayConfig",
    "HTTPUpstream",
    "INLINE_CONFIG_ENV",
    "InlineGatewayConfig",
    "InvalidToken",
    "StdioUpstream",
    "TokenClaims",
    "TokenCodec",
    "UpstreamCredentials",
    "create_gateway",
    "create_stdio_gateway",
    "decode_key",
    "generate_key",
]
