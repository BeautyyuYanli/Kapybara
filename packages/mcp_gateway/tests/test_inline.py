"""Private stdio launches consume their own inline targets/credentials without JWE."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from conftest import serve
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from mcp_types import TextContent, TextResourceContents
from pydantic import ValidationError

from mcp_gateway import INLINE_CONFIG_ENV, InlineGatewayConfig, create_stdio_gateway


async def test_each_stdio_launch_receives_inline_targets_and_credentials_without_jwe(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_JWE_KEY", "deliberately-invalid-and-unused")
    backend = FastMCP(
        "authenticated upstream",
        auth=StaticTokenVerifier(
            tokens={
                "alice-secret": {"client_id": "alice", "scopes": []},
                "bob-secret": {"client_id": "bob", "scopes": []},
            }
        ),
    )

    @backend.tool
    def identity() -> str:
        token = get_access_token()
        assert token is not None
        return token.client_id

    @backend.resource("data://info")
    def info() -> str:
        return "resource"

    @backend.prompt
    def greet(name: str) -> str:
        return f"hello {name}"

    async with serve(backend.http_app()) as url:

        async def use(user: str) -> None:
            config = InlineGatewayConfig.model_validate(
                {
                    "servers": {user: {"transport": "http", "url": f"{url}/mcp"}},
                    "credentials": {user: {"bearer": f"{user}-secret"}},
                }
            )
            transport = StdioTransport(
                sys.executable,
                ["-m", "mcp_gateway", "stdio"],
                env={INLINE_CONFIG_ENV: config.startup_json()},
                keep_alive=False,
            )
            async with Client(transport) as client:
                assert {tool.name for tool in await client.list_tools()} == {f"{user}.identity"}
                assert (await client.call_tool(f"{user}.identity")).data == user
                resource = (await client.read_resource(f"mcp-gateway://{user}/data://info"))[0]
                assert isinstance(resource, TextResourceContents) and resource.text == "resource"
                prompt = await client.get_prompt(f"{user}.greet", {"name": user})
                content = prompt.messages[0].content
                assert isinstance(content, TextContent) and content.text == f"hello {user}"

        await asyncio.gather(use("alice"), use("bob"))


async def test_stdio_cli_accepts_inline_argument_and_empty_configuration():
    transport = StdioTransport(
        sys.executable,
        ["-m", "mcp_gateway", "stdio", "--config-json", '{"servers":{}}'],
        keep_alive=False,
    )
    async with Client(transport) as client:
        assert await client.list_tools() == []
        assert await client.list_resources() == []
    assert create_stdio_gateway(InlineGatewayConfig()).auth is None


def test_startup_export_preserves_secrets_without_changing_normal_redaction():
    config = InlineGatewayConfig.model_validate(
        {
            "servers": {
                "api": {
                    "transport": "http",
                    "url": "http://localhost/mcp",
                    "headers": {"X-Key": "header-secret"},
                },
                "local": {
                    "transport": "stdio",
                    "command": "python",
                    "env": {"FIXED_KEY": "env-secret"},
                    "credential_env": ["API_KEY"],
                },
            },
            "credentials": {
                "api": {"bearer": "bearer-secret"},
                "local": {"env": {"API_KEY": "credential-secret"}},
            },
        }
    )
    normal = repr(config) + config.model_dump_json()
    for secret in ("header-secret", "env-secret", "bearer-secret", "credential-secret"):
        assert secret not in normal and secret in config.startup_json()
    assert InlineGatewayConfig.model_validate_json(config.startup_json()) == config


@pytest.mark.parametrize(
    "config",
    [
        {"credentials": {"missing": {}}},
        {
            "servers": {"local": {"transport": "stdio", "command": "python"}},
            "credentials": {"local": {"env": {"PATH": "unapproved"}}},
        },
    ],
)
def test_inline_configuration_rejects_unusable_credentials_before_launch(config):
    with pytest.raises(ValidationError):
        InlineGatewayConfig.model_validate(config)


def test_invalid_stdio_startup_keeps_stdout_clean_and_errors_redacted():
    config = {"servers": {}, "credentials": {"missing": {"bearer": "input-sensitive-value"}}}
    result = subprocess.run(
        [sys.executable, "-m", "mcp_gateway", "stdio"],
        env=os.environ | {INLINE_CONFIG_ENV: json.dumps(config)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and result.stdout == ""
    assert "input-sensitive-value" not in result.stderr
