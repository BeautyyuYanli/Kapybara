"""FastMCP aggregation with request-scoped upstream credentials and connections.

Each lookup creates a fresh ProxyProvider, so neither upstream sessions nor
credential-dependent component metadata can cross downstream requests. Proxied
components retain their own grant for execution. Tokens are never forwarded.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TypeVar

import httpx2
from fastmcp import FastMCP
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from fastmcp.prompts.base import Prompt
from fastmcp.resources.base import Resource
from fastmcp.resources.template import ResourceTemplate
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.server.providers import Provider
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider
from fastmcp.server.transforms.namespace import Namespace
from fastmcp.tools.base import Tool
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from .models import (
    GatewayConfig,
    HTTPUpstream,
    InlineGatewayConfig,
    Upstream,
    UpstreamCredentials,
    validate_credential_transport,
)
from .tokens import TokenCodec

AuthFactory = Callable[[HTTPUpstream, UpstreamCredentials], httpx2.Auth]
T = TypeVar("T")


def validate_grants(
    config: GatewayConfig | InlineGatewayConfig,
    upstreams: Mapping[str, UpstreamCredentials],
    auth_factories: Mapping[str, AuthFactory],
) -> None:
    """Reject unknown routes and credentials that the configured transport cannot use."""
    for name, grant in upstreams.items():
        server = config.servers.get(name)
        if server is None:
            raise ValueError("Grant references an unconfigured upstream")
        validate_credential_transport(server, grant)
        if grant.custom and name not in auth_factories:
            raise ValueError("HTTP grants require a configured adapter for custom authentication")
        if name in auth_factories and (
            grant.bearer is not None or grant.basic is not None or "authorization" in grant.headers
        ):
            raise ValueError(
                "An auth adapter cannot be combined with another Authorization mechanism"
            )


class GatewayAccessToken(AccessToken):
    upstreams: dict[str, UpstreamCredentials] = Field(repr=False, exclude=True)


class JWETokenVerifier(TokenVerifier):
    """Only fully validated grants enter FastMCP's authenticated request context."""

    def __init__(
        self, config: GatewayConfig, codec: TokenCodec, auth_factories: Mapping[str, AuthFactory]
    ) -> None:
        super().__init__()
        self.config = config
        self.codec = codec
        self.auth_factories = auth_factories

    async def verify_token(self, token: str) -> GatewayAccessToken | None:
        try:
            claims = self.codec.decode(token)
            validate_grants(self.config, claims.upstreams, self.auth_factories)
        except ValueError:
            return None
        return GatewayAccessToken(
            token=token,
            client_id=claims.jti,
            subject=claims.sub,
            scopes=[],
            expires_at=claims.exp,
            upstreams=claims.upstreams,
            claims={"iss": claims.iss, "sub": claims.sub, "jti": claims.jti},
        )


class DotNamespace(Namespace):
    """Dot-separated names and reversible URI wrapping, including URNs and templates."""

    def __init__(self, prefix: str) -> None:
        super().__init__(prefix)
        self.prefix = prefix

    def _transform_name(self, name: str) -> str:
        return f"{self.prefix}.{name}"

    def _reverse_name(self, name: str) -> str | None:
        prefix = f"{self.prefix}."
        return name[len(prefix) :] if name.startswith(prefix) else None

    def _transform_uri(self, uri: str) -> str:
        return f"mcp-gateway://{self.prefix}/{uri}"

    def _reverse_uri(self, uri: str) -> str | None:
        prefix = f"mcp-gateway://{self.prefix}/"
        return uri[len(prefix) :] if uri.startswith(prefix) else None


def _client(
    server: Upstream, grant: UpstreamCredentials, auth_factory: AuthFactory | None
) -> ProxyClient:
    context = get_context().request_context
    mode = server.mode or (
        context.protocol_version
        if context is not None and context.protocol_version in MODERN_PROTOCOL_VERSIONS
        else "legacy"
    )
    if isinstance(server, HTTPUpstream):
        headers = {name: value.get_secret_value() for name, value in server.headers.items()}
        headers.update({name: value.get_secret_value() for name, value in grant.headers.items()})
        auth: httpx2.Auth | str | None = None
        if auth_factory is not None:
            try:
                auth = auth_factory(server, grant)
            except Exception:
                raise RuntimeError("Upstream authentication adapter failed") from None
            # The adapter owns Authorization, including when config has a default.
            headers.pop("authorization", None)
        elif grant.bearer is not None:
            auth = grant.bearer.get_secret_value()
            headers.pop("authorization", None)
        elif grant.basic is not None:
            auth = httpx2.BasicAuth(grant.basic.username, grant.basic.password.get_secret_value())
            headers.pop("authorization", None)
        transport_type = StreamableHttpTransport if server.transport == "http" else SSETransport
        transport = transport_type(str(server.url), headers=headers, auth=auth)
    else:
        env = {name: value.get_secret_value() for name, value in server.env.items()}
        env.update({name: value.get_secret_value() for name, value in grant.env.items()})
        transport = StdioTransport(
            server.command,
            server.args,
            env=env,
            cwd=server.cwd,
            keep_alive=False,
        )
    return ProxyClient(
        transport,
        forward_incoming_headers=False,
        timeout=server.timeout,
        mode=mode,
    )


class GrantedProvider(Provider):
    """Gate discovery and lookup before connecting, with no shared proxy metadata."""

    def __init__(
        self,
        name: str,
        server: Upstream,
        auth_factory: AuthFactory | None,
        *,
        grant: UpstreamCredentials | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self.server = server
        self.auth_factory = auth_factory
        self.grant = grant

    def _proxy(self) -> ProxyProvider | None:
        grant = self.grant
        if grant is None:
            token = get_access_token()
            if not isinstance(token, GatewayAccessToken) or self.name not in token.upstreams:
                return None
            grant = token.upstreams[self.name]
        provider = ProxyProvider(
            lambda: _client(self.server, grant, self.auth_factory),
            cache_ttl=0,
        )
        provider.add_transform(DotNamespace(self.name))
        return provider

    async def _query(self, query: Callable[[ProxyProvider], Awaitable[T]], empty: T) -> T:
        try:
            proxy = self._proxy()
            return await query(proxy) if proxy else empty
        except Exception:
            # FastMCP's execution masking does not cover component discovery.
            raise MCPError(
                code=INTERNAL_ERROR,
                message=f"Upstream '{self.name}' request failed",
            ) from None

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        # Proxy components forbid tasks. Startup enumeration has no request
        # context and must not discover/connect upstreams before the pipe opens.
        return []

    async def _list_tools(self) -> Sequence[Tool]:
        return await self._query(lambda proxy: proxy.list_tools(), [])

    async def _get_tool(self, name: str, version: VersionSpec | None = None) -> Tool | None:
        return await self._query(lambda proxy: proxy.get_tool(name, version=version), None)

    async def _list_resources(self) -> Sequence[Resource]:
        return await self._query(lambda proxy: proxy.list_resources(), [])

    async def _get_resource(self, uri: str, version: VersionSpec | None = None) -> Resource | None:
        return await self._query(lambda proxy: proxy.get_resource(uri, version=version), None)

    async def _list_resource_templates(self) -> Sequence[ResourceTemplate]:
        return await self._query(lambda proxy: proxy.list_resource_templates(), [])

    async def _get_resource_template(
        self, uri: str, version: VersionSpec | None = None
    ) -> ResourceTemplate | None:
        return await self._query(
            lambda proxy: proxy.get_resource_template(uri, version=version), None
        )

    async def _list_prompts(self) -> Sequence[Prompt]:
        return await self._query(lambda proxy: proxy.list_prompts(), [])

    async def _get_prompt(self, name: str, version: VersionSpec | None = None) -> Prompt | None:
        return await self._query(lambda proxy: proxy.get_prompt(name, version=version), None)


def create_gateway(
    config: GatewayConfig,
    codec: TokenCodec,
    *,
    auth_factories: Mapping[str, AuthFactory] | None = None,
) -> FastMCP:
    """Return an authenticated FastMCP server for HTTP serving or ASGI mounting.

    Factories are trusted Python adapters, selected by configured namespace; tokens
    cannot supply code or destinations. Upstream MCP sessions last one request, so
    stateful multi-call workflows must store their state outside the MCP connection.
    By default protocol eras mirror the frontend; server.mode can explicitly pin
    a legacy backend for clients whose ordinary calls do not need era-specific features.
    """
    if config.issuer != codec.issuer or config.audience != codec.audience:
        raise ValueError("Gateway configuration and codec must agree on issuer and audience")
    config = config.model_copy(deep=True)
    factories = dict(auth_factories or {})
    if any(
        name not in config.servers or not isinstance(config.servers[name], HTTPUpstream)
        for name in factories
    ):
        raise ValueError("Auth factories must refer to configured HTTP/SSE upstreams")
    gateway = FastMCP(
        config.name,
        auth=JWETokenVerifier(config, codec, factories),
        providers=[
            GrantedProvider(name, server, factories.get(name))
            for name, server in config.servers.items()
        ],
        mask_error_details=True,
    )
    gateway.provider_error_strategy = "raise"
    return gateway


def create_stdio_gateway(
    config: InlineGatewayConfig,
    *,
    auth_factories: Mapping[str, AuthFactory] | None = None,
) -> FastMCP:
    """Build a private, unauthenticated stdio server from this launch's configuration.

    This API needs no JWE, issuer, encryption key or global upstream registry.
    The parent owns the pipe and supplies all targets/credentials before startup.
    Source models are copied; upstream sessions remain isolated per request.
    """
    config = config.model_copy(deep=True)
    factories = dict(auth_factories or {})
    if any(
        name not in config.servers or not isinstance(config.servers[name], HTTPUpstream)
        for name in factories
    ):
        raise ValueError("Auth factories must refer to configured HTTP/SSE upstreams")
    grants = config.grants()
    validate_grants(config, grants, factories)
    gateway = FastMCP(
        config.name,
        providers=[
            GrantedProvider(name, server, factories.get(name), grant=grants[name])
            for name, server in config.servers.items()
        ],
        mask_error_details=True,
    )
    gateway.provider_error_strategy = "raise"
    return gateway
