"""Model-compatible aliases with native SDK renaming and MCP dispatch."""

import re
from dataclasses import dataclass
from hashlib import sha256

from pydantic_ai import RunContext
from pydantic_ai.exceptions import UserError
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import RenamedToolset, ToolsetTool


def model_tool_name(name: str) -> str:
    """Keep safe names; make stable, bounded aliases for dots and long MCP names."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return name
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:47]
    return f"{stem}_{sha256(name.encode()).hexdigest()[:16]}"


@dataclass
class Toolset[DepsT](RenamedToolset[DepsT]):
    """A native MCPToolset wrapped for models with stricter tool-name limits.

    The entered MCPToolset caches discovery. RenamedToolset owns schema rewriting
    and reverse dispatch; alias data belongs only to this run/Activity instance.
    The wrapped client remains available for direct calls using original MCP names.
    """

    @property
    def mcp(self) -> MCPToolset[DepsT]:
        assert isinstance(self.wrapped, MCPToolset)
        return self.wrapped

    @property
    def id(self) -> str | None:
        return self.wrapped.id

    async def get_tools(self, ctx: RunContext[DepsT]) -> dict[str, ToolsetTool[DepsT]]:
        async with self.mcp:
            names = [tool.name for tool in await self.mcp.list_tools()]
            aliases = {model_tool_name(name): name for name in names}
            if len(aliases) != len(names):
                raise UserError("MCP tools have conflicting model aliases")
            self.name_map = aliases
            return await super().get_tools(ctx)
