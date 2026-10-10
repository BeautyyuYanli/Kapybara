"""Run the same Agent with different JSON layer selections.

Execute with ``python -m kapy.layers.examples.in_process``.
"""

import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from kapy.layers import LayerDeps, layers_capability, load_layers
from kapy.layers.examples import echo
from kapy.layers.types import JsonObject


async def main() -> None:
    registry = {"echo": echo}
    agent = Agent(TestModel(), deps_type=LayerDeps, capabilities=[layers_capability(registry)])
    configs: list[JsonObject] = [{"prefix": "A:"}, {"prefix": "B:", "uppercase": True}]
    for config in configs:
        deps = LayerDeps(layers=load_layers({"echo": config}, {}, registry))
        print((await agent.run("回显文本", deps=deps)).output)
    print((await agent.run("普通回答", deps=LayerDeps())).output)


if __name__ == "__main__":
    asyncio.run(main())
