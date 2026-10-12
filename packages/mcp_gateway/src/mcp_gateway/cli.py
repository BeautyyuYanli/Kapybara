"""Private inline stdio, offline token issuance and authenticated HTTP serving."""

import argparse
import os
from pathlib import Path

from pydantic import TypeAdapter

from .gateway import create_gateway, create_stdio_gateway, validate_grants
from .models import (
    INLINE_CONFIG_ENV,
    GatewayConfig,
    InlineGatewayConfig,
    NamespaceName,
    UpstreamCredentials,
)
from .tokens import TokenCodec, decode_key, generate_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate MCP servers over stdio or HTTP/JWE")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen", help="Print a random base64url 256-bit JWE key")
    stdio = commands.add_parser(
        "stdio", help="Run a private gateway with inline startup configuration"
    )
    stdio.add_argument("--config-json", help=f"Inline JSON; defaults to {INLINE_CONFIG_ENV}")
    serve = commands.add_parser("serve", help="Run the Streamable HTTP gateway")
    issue = commands.add_parser(
        "issue", help="Encrypt a credentials JSON file into one bearer token"
    )
    for command in (serve, issue):
        command.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8001)
    serve.add_argument("--path", default="/mcp")
    issue.add_argument("--subject", required=True)
    issue.add_argument("--credentials", type=Path, required=True)
    issue.add_argument("--ttl", type=int, default=3600, help="Lifetime in seconds, at most 86400")
    args = parser.parse_args()
    if args.command == "keygen":
        print(generate_key())
        return
    if args.command == "stdio":
        try:
            inline_json = (
                args.config_json
                if args.config_json is not None
                else os.environ.get(INLINE_CONFIG_ENV)
            )
            if inline_json is None:
                raise ValueError("Missing inline configuration")
            inline = InlineGatewayConfig.model_validate_json(inline_json)
            gateway = create_stdio_gateway(inline)
        except ValueError:
            parser.exit(2, "Invalid or missing inline MCP gateway configuration\n")
        gateway.run(transport="stdio", show_banner=False)
        return
    try:
        config = GatewayConfig.model_validate_json(args.config.read_text())
        encoded_key = os.environ.get("MCP_GATEWAY_JWE_KEY")
        if not encoded_key:
            raise ValueError("Set MCP_GATEWAY_JWE_KEY to a key generated with keygen")
        codec = TokenCodec(
            {"primary": decode_key(encoded_key)},
            active_key_id="primary",
            issuer=config.issuer,
            audience=config.audience,
        )
        if args.command == "issue":
            grants = TypeAdapter(dict[NamespaceName, UpstreamCredentials]).validate_json(
                args.credentials.read_text()
            )
            validate_grants(config, grants, {})
            print(codec.issue(args.subject, grants, ttl_seconds=args.ttl))
            return
    except (OSError, ValueError):
        # Validation/OSError details may contain credentials, paths or URLs.
        parser.exit(2, "Invalid configuration, credentials, lifetime or MCP_GATEWAY_JWE_KEY\n")
    if not args.path.startswith("/") or not 1 <= args.port <= 65535:
        parser.error("Provide an absolute --path and a port between 1 and 65535")
    create_gateway(config, codec).run(
        transport="http",
        host=args.host,
        port=args.port,
        path=args.path,
    )
