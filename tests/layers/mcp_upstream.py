"""A real stdio MCP fixture records externally observable process ownership."""

import asyncio
import json
import os
from pathlib import Path

from fastmcp import FastMCP


def record(event: str) -> None:
    with Path(os.environ["PID_FILE"]).open("a") as file:
        file.write(json.dumps({"event": event, "pid": os.getpid(), "parent": os.getppid()}) + "\n")


record("start")
server = FastMCP("layer upstream")


@server.tool
def echo(text: str) -> str:
    record("echo")
    assert "MCP_GATEWAY_INLINE_CONFIG" not in os.environ
    assert "MCP_GATEWAY_JWE_KEY" not in os.environ
    return f"{os.environ['LABEL']}:{text}:{os.environ['API_KEY']}"


@server.tool
async def block() -> str:
    record("block")
    await asyncio.sleep(3600)
    return "completed"


@server.tool
def fail() -> str:
    raise ValueError("deliberate fixture failure")


def add_named_tool(name: str) -> None:
    def run() -> str:
        return name

    server.tool(run, name=name, description=f"alias:{name}")


for name in ("a.b", "a_b", "x" * 64):
    add_named_tool(name)


if __name__ == "__main__":
    server.run(transport="stdio", show_banner=False)
