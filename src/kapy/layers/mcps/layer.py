"""MCP tools through one run-local stdio gateway, with native SDK lifecycle and aliases.

Assembly registers a stable DynamicToolset definition without launching a process.
The SDK owns each stdio Gateway: per run for ordinary Agents, per Activity for
Temporal discovery and tool calls. Workflow replay performs no MCP I/O.
No definition retains config, credentials, execution deps or connected clients.
"""

import sys

from fastmcp.client.transports import StdioTransport
from mcp_gateway import INLINE_CONFIG_ENV
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import DynamicToolset

from kapy.layers.types import Layer, LayerAccess

from .models import Config, State
from .toolset import Toolset


def make_toolset[DepsT](config: Config, *, id: str = "mcps") -> Toolset[DepsT]:
    """Build an unconnected MCPToolset with a model-name wrapper.

    Configuration travels in the child environment, keeping stdin/stdout available
    exclusively for MCP messages. The gateway does not authenticate this pipe.
    Keep-alive is disabled so success, failure and cancellation close the process.
    """
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "mcp_gateway", "stdio"],
        env={INLINE_CONFIG_ENV: config.startup_json()},
        keep_alive=False,
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
        return make_toolset(config, id=layer_id)

    return Layer(
        config_type=Config,
        state_type=State,
        capability=Capability(
            id=layer_id,
            toolsets=[DynamicToolset(toolset, per_run_step=False, id=layer_id)],
        ),
    )
