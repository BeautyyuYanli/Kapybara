"""Run with different plugin configs on the same Agent, then disable them.

Execute with ``python -m kapy.capability_plugins.examples.in_process``.
"""

import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from kapy.capability_plugins import PluginDeps, PluginRegistry, PluginSpec
from kapy.capability_plugins.examples.echo import EchoConfig, build_echo


async def main() -> None:
    registry = PluginRegistry[PluginDeps]()
    registry.register("echo", config_type=EchoConfig, factory=build_echo)
    agent = Agent(
        TestModel(),
        deps_type=PluginDeps,
        capabilities=[registry.capability(id="plugins")],
    )

    for config in ({"prefix": "A:"}, {"prefix": "B:", "uppercase": True}):
        result = await agent.run(
            "回显文本",
            deps=PluginDeps(plugins=(PluginSpec(plugin_name="echo", config=config),)),
        )
        print(result.output)

    result = await agent.run("普通回答", deps=PluginDeps())
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
