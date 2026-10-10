"""Run the same Agent with different JSON layer selections.

Execute with ``python -m kapy.layers.examples.in_process``.
"""

import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from kapy.layers import LayerData, LayerDeps, layers_capability, load_layers, make_layers
from kapy.layers.examples import echo
from kapy.layers.types import JsonObject


async def main() -> None:
    registry = {"echo": echo.make}

    def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
        return deps.layers

    layers = make_layers(registry, get_layers)
    agent = Agent(
        TestModel(),
        deps_type=LayerDeps,
        capabilities=[layers_capability(layers, get_layers=get_layers)],
    )
    configs: list[JsonObject] = [{"prefix": "A:"}, {"prefix": "B:", "uppercase": True}]
    for config in configs:
        deps = LayerDeps(layers=load_layers({"echo": config}, {}, layers))
        print((await agent.run("回显文本", deps=deps)).output)
    print((await agent.run("普通回答", deps=LayerDeps())).output)


if __name__ == "__main__":
    asyncio.run(main())
