"""Select from statically assembled layers using the same Agent and different JSON deps.

Execute with ``python -m pydantic_ai_layer_protocol.examples.in_process``.
"""

import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from pydantic_ai_layer_protocol import LayerData, LayerDeps, LayerRegistry
from pydantic_ai_layer_protocol.examples import echo
from pydantic_ai_layer_protocol.types import JsonObject


async def main() -> None:
    registry = {"echo": echo.make}

    def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
        return deps.layers

    layers = LayerRegistry(registry, get_layers=get_layers)
    agent = Agent(
        TestModel(),
        deps_type=LayerDeps,
        capabilities=[layers],
    )
    configs: list[JsonObject] = [{"prefix": "A:"}, {"prefix": "B:", "uppercase": True}]
    for config in configs:
        deps = LayerDeps(layers=layers.load({"echo": config}, {}))
        print((await agent.run("回显文本", deps=deps)).output)
    print((await agent.run("普通回答", deps=LayerDeps())).output)


if __name__ == "__main__":
    asyncio.run(main())
