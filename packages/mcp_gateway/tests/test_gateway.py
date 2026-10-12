"""Exercise aggregation and identity boundaries over actual MCP transports."""

import asyncio
import base64
import os
import sys
from collections.abc import Generator
from pathlib import Path

import httpx2
import pytest
from conftest import serve
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token, get_context, get_http_headers
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    TextContent,
    TextResourceContents,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway import GatewayConfig, HTTPUpstream, UpstreamCredentials, create_gateway


def upstream(label: str, auth=None) -> FastMCP:
    server = FastMCP(label, auth=auth)

    @server.tool
    async def identity() -> str:
        await asyncio.sleep(0.005)
        token = get_access_token()
        return f"{label}:{token.client_id if token else 'public'}"

    @server.resource("data://status")
    def status() -> str:
        return label

    @server.resource("urn:example:status")
    def urn_status() -> str:
        return f"urn:{label}"

    @server.resource("data://items/{item}")
    def item(item: str) -> str:
        return f"{label}:{item}"

    @server.prompt
    def greet(name: str) -> str:
        return f"{label}:hello {name}"

    @server.tool
    def fail() -> str:
        raise ValueError("upstream-internal-secret")

    return server


def record_headers(app: ASGIApp, requests: list[dict[str, str]]) -> ASGIApp:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            requests.append({name.decode(): value.decode() for name, value in scope["headers"]})
        await app(scope, receive, send)

    return wrapped


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_namespaced_tools_prompts_resources_and_sse(codec, mode):
    one, two = upstream("one"), upstream("two")
    async with serve(one.http_app()) as one_url, serve(two.http_app(transport="sse")) as two_url:
        config = GatewayConfig.model_validate(
            {
                "servers": {
                    "one": {"transport": "http", "url": f"{one_url}/mcp"},
                    "two": {"transport": "sse", "url": f"{two_url}/sse"},
                }
            }
        )
        gateway = create_gateway(config, codec)
        token = codec.issue("alice", {name: UpstreamCredentials() for name in config.servers})
        async with serve(gateway.http_app()) as gateway_url:
            async with Client(f"{gateway_url}/mcp", auth=token, mode=mode) as client:
                assert {t.name for t in await client.list_tools()} == {
                    "one.identity",
                    "two.identity",
                    "one.fail",
                    "two.fail",
                }
                assert (await client.call_tool("one.identity")).data == "one:public"
                assert (await client.call_tool("two.identity")).data == "two:public"
                assert {p.name for p in await client.list_prompts()} == {"one.greet", "two.greet"}
                prompt = await client.get_prompt("two.greet", {"name": "Mei"})
                content = prompt.messages[0].content
                assert isinstance(content, TextContent) and content.text == "two:hello Mei"
                assert {str(r.uri) for r in await client.list_resources()} == {
                    f"mcp-gateway://{name}/{uri}"
                    for name in ("one", "two")
                    for uri in ("data://status", "urn:example:status")
                }
                assert {t.uri_template for t in await client.list_resource_templates()} == {
                    "mcp-gateway://one/data://items/{item}",
                    "mcp-gateway://two/data://items/{item}",
                }
                for uri, expected in (
                    ("mcp-gateway://one/data://status", "one"),
                    ("mcp-gateway://two/urn:example:status", "urn:two"),
                    ("mcp-gateway://two/data://items/42", "two:42"),
                ):
                    content = (await client.read_resource(uri))[0]
                    assert isinstance(content, TextResourceContents) and content.text == expected
                with pytest.raises(ToolError):
                    await client.call_tool("one.fail")


async def test_bearer_discovery_permissions_and_concurrent_isolation(codec):
    backend = upstream(
        "backend",
        StaticTokenVerifier(
            tokens={
                "alice-key": {"client_id": "alice", "scopes": ["private"]},
                "bob-key": {"client_id": "bob", "scopes": []},
            }
        ),
    )

    @backend.tool(auth=require_scopes("private"))
    def private() -> str:
        return "private data"

    requests: list[dict[str, str]] = []
    async with serve(record_headers(backend.http_app(), requests)) as backend_url:
        config = GatewayConfig.model_validate(
            {
                "servers": {
                    "backend": {"transport": "http", "url": f"{backend_url}/mcp"},
                    "hidden": {"transport": "http", "url": "http://127.0.0.1:1/mcp"},
                }
            }
        )
        gateway = create_gateway(config, codec)
        tokens = {
            user: codec.issue(
                user,
                {
                    "backend": UpstreamCredentials.model_validate(
                        {
                            "bearer": f"{user}-key",
                            "headers": {"X-Tenant": user},
                        }
                    )
                },
            )
            for user in ("alice", "bob")
        }
        async with serve(gateway.http_app()) as gateway_url:
            transport = StreamableHttpTransport(
                f"{gateway_url}/mcp",
                auth=tokens["alice"],
                headers={
                    "X-Downstream-Secret": "must-not-forward",
                    "Cookie": "must-not-forward",
                },
            )
            async with (
                Client(transport) as alice,
                Client(f"{gateway_url}/mcp", auth=tokens["bob"]) as bob,
            ):
                assert "backend.private" in {t.name for t in await alice.list_tools()}
                assert "backend.private" not in {t.name for t in await bob.list_tools()}
                assert {p.name for p in await alice.list_prompts()} == {"backend.greet"}
                assert all(
                    str(r.uri).startswith("mcp-gateway://backend/")
                    for r in await alice.list_resources()
                )
                assert len(await alice.list_resource_templates()) == 1
                with pytest.raises(ToolError):
                    await bob.call_tool("backend.private")
                with pytest.raises(ToolError):
                    await alice.call_tool("hidden.identity")
                with pytest.raises(MCPError):
                    await alice.get_prompt("hidden.greet", {"name": "Mei"})
                with pytest.raises(MCPError):
                    await alice.read_resource("mcp-gateway://hidden/data://status")
                results = await asyncio.gather(
                    *(
                        client.call_tool("backend.identity")
                        for _ in range(4)
                        for client in (alice, bob)
                    )
                )
                assert [r.data for r in results] == ["backend:alice", "backend:bob"] * 4
    assert requests
    assert all(r.get("authorization") in {"Bearer alice-key", "Bearer bob-key"} for r in requests)
    assert all("cookie" not in r and "x-downstream-secret" not in r for r in requests)
    assert all(r.get("x-tenant") in {"alice", "bob"} for r in requests)


@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_header_basic_and_custom_authentication(codec, transport):
    backend = FastMCP("header backend")
    versions: list[str] = []

    @backend.tool
    def headers() -> dict[str, str]:
        incoming = get_http_headers(include={"authorization"})
        context = get_context().request_context
        assert context is not None
        versions.append(context.protocol_version)
        return {name: incoming.get(name, "") for name in ("authorization", "x-api-key", "x-signed")}

    class SigningAuth(httpx2.Auth):
        def __init__(self, secret: str) -> None:
            self.secret = secret

        def auth_flow(
            self, request: httpx2.Request
        ) -> Generator[httpx2.Request, httpx2.Response, None]:
            request.headers["X-Signed"] = self.secret
            yield request

    def factory(server: HTTPUpstream, grant: UpstreamCredentials) -> httpx2.Auth:
        value = grant.custom["signing_key"]
        assert isinstance(value, str)
        return SigningAuth(value)

    async with serve(backend.http_app(transport=transport)) as backend_url:
        endpoint = f"{backend_url}/{'mcp' if transport == 'http' else 'sse'}"
        config = GatewayConfig.model_validate(
            {
                "servers": {
                    name: {"transport": transport, "url": endpoint, "mode": "legacy"}
                    for name in ("header", "basic", "custom")
                }
            }
        )
        gateway = create_gateway(config, codec, auth_factories={"custom": factory})
        token = codec.issue(
            "alice",
            {
                "header": UpstreamCredentials.model_validate(
                    {"headers": {"X-API-Key": "api-secret"}}
                ),
                "basic": UpstreamCredentials.model_validate(
                    {
                        "basic": {"username": "alice", "password": "password-secret"},
                    }
                ),
                "custom": UpstreamCredentials(custom={"signing_key": "signing-secret"}),
            },
        )
        async with serve(gateway.http_app()) as gateway_url:
            async with Client(f"{gateway_url}/mcp", auth=token) as client:
                header = (await client.call_tool("header.headers")).data
                assert header == {"authorization": "", "x-api-key": "api-secret", "x-signed": ""}
                basic = (await client.call_tool("basic.headers")).data
                assert (
                    basic["authorization"]
                    == "Basic " + base64.b64encode(b"alice:password-secret").decode()
                )
                custom = (await client.call_tool("custom.headers")).data
                assert custom == {
                    "authorization": "",
                    "x-api-key": "",
                    "x-signed": "signing-secret",
                }
    assert versions and all(version not in MODERN_PROTOCOL_VERSIONS for version in versions)


async def test_upstream_outage_is_reported_and_other_namespaces_remain_callable(codec):
    async with serve(upstream("healthy").http_app()) as backend_url:
        config = GatewayConfig.model_validate(
            {
                "servers": {
                    "healthy": {"transport": "http", "url": f"{backend_url}/mcp"},
                    "offline": {"transport": "http", "url": "http://127.0.0.1:1/mcp"},
                }
            }
        )
        gateway = create_gateway(config, codec)
        token = codec.issue("alice", {name: UpstreamCredentials() for name in config.servers})
        async with serve(gateway.http_app()) as gateway_url:
            async with Client(f"{gateway_url}/mcp", auth=token) as client:
                with pytest.raises(MCPError, match="Upstream 'offline' request failed"):
                    await client.list_tools()
                # The convenience SDK re-lists all tools to validate output schemas.
                # A protocol request checks routing independently of that failed catalog.
                result = await client.session.send_request(
                    CallToolRequest(
                        method="tools/call", params=CallToolRequestParams(name="healthy.identity")
                    ),
                    CallToolResult,
                )
                content = result.content[0]
                assert isinstance(content, TextContent) and content.text == "healthy:public"


async def test_adapter_exceptions_do_not_reveal_credentials(codec):
    config = GatewayConfig.model_validate(
        {
            "servers": {
                "custom": {"transport": "http", "url": "http://127.0.0.1:1/mcp"},
            }
        }
    )

    def factory(server: HTTPUpstream, grant: UpstreamCredentials) -> httpx2.Auth:
        raise ValueError(f"adapter leaked {grant.custom['secret']}")

    gateway = create_gateway(config, codec, auth_factories={"custom": factory})
    token = codec.issue(
        "alice",
        {
            "custom": UpstreamCredentials(custom={"secret": "upstream-sensitive-value"}),
        },
    )
    async with serve(gateway.http_app()) as gateway_url:
        async with Client(f"{gateway_url}/mcp", auth=token) as client:
            for query in (
                client.list_tools,
                client.list_resources,
                client.list_prompts,
                client.list_resource_templates,
            ):
                with pytest.raises(MCPError) as failure:
                    await query()
                assert "upstream-sensitive-value" not in str(failure.value)
                assert "custom" in str(failure.value)


async def test_invalid_grants_are_401_and_never_contact_upstream(codec):
    requests: list[dict[str, str]] = []
    async with serve(record_headers(upstream("backend").http_app(), requests)) as backend_url:
        config = GatewayConfig.model_validate(
            {
                "servers": {
                    "backend": {"transport": "http", "url": f"{backend_url}/mcp"},
                    "local": {
                        "transport": "stdio",
                        "command": "invalid",
                        "credential_env": ["API_KEY"],
                    },
                }
            }
        )
        gateway = create_gateway(config, codec)
        invalid = [
            None,
            "malformed",
            codec.issue("alice", {"unknown": UpstreamCredentials()}),
            codec.issue(
                "alice", {"backend": UpstreamCredentials(custom={"secret": "hidden-secret"})}
            ),
            codec.issue(
                "alice", {"backend": UpstreamCredentials.model_validate({"env": {"API_KEY": "s"}})}
            ),
            codec.issue(
                "alice", {"local": UpstreamCredentials.model_validate({"env": {"PATH": "s"}})}
            ),
        ]
        async with serve(gateway.http_app()) as gateway_url:
            async with httpx2.AsyncClient() as client:
                for token in invalid:
                    response = await client.post(
                        f"{gateway_url}/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "ping",
                        },
                        headers={"Authorization": f"Bearer {token}"} if token else {},
                    )
                    assert response.status_code == 401 and "hidden-secret" not in response.text
    assert requests == []


async def test_session_id_cannot_be_reused_with_another_token_for_same_subject(codec):
    async with serve(upstream("backend").http_app()) as backend_url:
        config = GatewayConfig.model_validate(
            {
                "servers": {
                    "backend": {"transport": "http", "url": f"{backend_url}/mcp"},
                }
            }
        )
        gateway = create_gateway(config, codec)
        first = codec.issue("alice", {"backend": UpstreamCredentials()})
        second = codec.issue("alice", {"backend": UpstreamCredentials()})
        async with serve(gateway.http_app()) as gateway_url:
            transport = StreamableHttpTransport(f"{gateway_url}/mcp", auth=first)
            async with Client(transport, mode="legacy"):
                session_id = transport.get_session_id()
                assert session_id
                async with httpx2.AsyncClient() as client:
                    response = await client.post(
                        f"{gateway_url}/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "ping",
                        },
                        headers={
                            "Authorization": f"Bearer {second}",
                            "Mcp-Session-Id": session_id,
                            "Accept": "application/json, text/event-stream",
                            "Mcp-Protocol-Version": "2025-11-25",
                        },
                    )
                    assert response.status_code == 404


async def test_stdio_credentials_and_process_cleanup(codec, tmp_path: Path):
    script = tmp_path / "backend.py"
    marker = tmp_path / "pids.txt"
    script.write_text("""
import os
from pathlib import Path
from fastmcp import FastMCP
with Path(os.environ["PID_FILE"]).open("a") as file:
    file.write(f"{os.getpid()}\\n")
server = FastMCP("stdio")
@server.tool
def identity() -> str:
    return os.environ["API_KEY"]
server.run(transport="stdio", show_banner=False)
""")
    config = GatewayConfig.model_validate(
        {
            "servers": {
                "local": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(script)],
                    "env": {"PID_FILE": str(marker)},
                    "credential_env": ["API_KEY"],
                },
            }
        }
    )
    gateway = create_gateway(config, codec)
    async with serve(gateway.http_app()) as gateway_url:
        for user in ("alice", "bob"):
            token = codec.issue(
                user,
                {
                    "local": UpstreamCredentials.model_validate(
                        {
                            "env": {"API_KEY": f"{user}-secret"},
                        }
                    )
                },
            )
            async with Client(f"{gateway_url}/mcp", auth=token) as client:
                assert (await client.call_tool("local.identity")).data == f"{user}-secret"
    pids = [int(value) for value in marker.read_text().splitlines()]
    assert len(set(pids)) >= 4
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize("namespace", ["a.b", "a/b", "a:b", "UPPER", "", "a" * 33])
def test_configuration_rejects_ambiguous_namespaces(namespace):
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(
            {
                "servers": {
                    namespace: {
                        "transport": "http",
                        "url": "http://127.0.0.1:9000/mcp",
                    }
                }
            }
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "host"},
        {"Mcp-Session-Id": "session"},
        {"Content-Length": "0"},
        {"X-Key": "a", "x-key": "b"},
        {"X-Key": "secret\r\nInjected: value"},
    ],
)
def test_credentials_reject_header_injection(headers):
    with pytest.raises(ValidationError):
        UpstreamCredentials.model_validate({"headers": headers})


def test_config_and_credentials_cannot_select_untrusted_code_or_locations():
    with pytest.raises(ValidationError):
        UpstreamCredentials.model_validate({"url": "http://untrusted.example", "bearer": "secret"})
    with pytest.raises(ValidationError):
        UpstreamCredentials.model_validate({"command": "untrusted"})
    with pytest.raises(ValidationError):
        UpstreamCredentials.model_validate({"bearer": "a", "headers": {"Authorization": "b"}})
    for url in (
        "file:///etc/passwd",
        "http://user:password@localhost/mcp",
        "http://localhost/mcp#fragment",
    ):
        with pytest.raises(ValidationError):
            HTTPUpstream.model_validate({"url": url})
