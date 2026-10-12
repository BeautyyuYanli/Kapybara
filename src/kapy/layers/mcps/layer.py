"""MCP tools through one run-local stdio gateway, with native SDK lifecycle and aliases.

Assembly registers a stable DynamicToolset definition without launching a process.
Ordinary runs own their stdio Gateway. Temporal Activities may connect to a
run-owned relay supplied by their context, owning only that short HTTP connection.
No definition retains config, credentials, execution deps or connected clients.
"""

import sys
from typing import Protocol, runtime_checkable

from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcp_gateway import INLINE_CONFIG_ENV
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import DynamicToolset

from kapy.layers.types import Layer, LayerAccess

from .models import Config, GatewayEndpoint, State
from .toolset import Toolset


@runtime_checkable
class GatewayContext(Protocol):
    @property
    def mcp_gateway(self) -> GatewayEndpoint | None: ...


def make_toolset[DepsT](
    config: Config, *, id: str = "mcps", gateway: GatewayEndpoint | None = None
) -> Toolset[DepsT]:
    """Build an unconnected MCPToolset with a model-name wrapper.

    Configuration travels in the child environment, keeping stdin/stdout available
    exclusively for MCP messages. The gateway does not authenticate this pipe.
    Keep-alive is disabled so success, failure and cancellation close the process.
    A supplied gateway connects to its run-owned relay instead; this client closes
    only its HTTP session, while the run owner retains the stdio Gateway.
    """
    transport = (
        StreamableHttpTransport(str(gateway.url), auth=gateway.token)
        if gateway is not None
        else StdioTransport(
            command=sys.executable,
            args=["-m", "mcp_gateway", "stdio"],
            env={INLINE_CONFIG_ENV: config.startup_json()},
            keep_alive=False,
        )
    )
    return Toolset(
        MCPToolset[DepsT](
            transport,
            id=id,
            init_timeout=config.init_timeout,
            read_timeout=config.read_timeout,
            prefer_tasks=False,
            cache_tools=True,
        ),
        name_map={},
    )


def make[DepsT](layer_id: str, access: LayerAccess[DepsT]) -> Layer[DepsT, Config, State]:
    """Contribute a fresh MCPToolset for each selected run, with empty JSON State."""

    def toolset(ctx: RunContext[DepsT]) -> Toolset[DepsT]:
        config = Config.model_validate(access(ctx.deps)["config"])
        gateway = ctx.mcp_gateway if isinstance(ctx, GatewayContext) else None
        return make_toolset(config, id=layer_id, gateway=gateway)

    return Layer(
        config_type=Config,
        state_type=State,
        capability=Capability(
            id=layer_id,
            toolsets=[DynamicToolset(toolset, per_run_step=False, id=layer_id)],
        ),
    )
