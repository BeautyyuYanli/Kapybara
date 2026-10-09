"""Sandboxed test workflow and Activity-only RPC peers, independent of Kapy."""

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from plugin_host import PluginHostCapability, PluginManifest, describe_plugins
    from pydantic import BaseModel, JsonValue
    from pydantic_ai import Agent
    from pydantic_ai.durable_exec.temporal import TemporalDurability
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel


class PluginDeps(BaseModel):
    manifests: list[PluginManifest]


rpc_calls: list[tuple[str, str, dict[str, JsonValue]]] = []
client_lookups: list[str] = []


class MemoryClient:
    """A protocol-correct peer whose transport is forbidden in Workflow code."""

    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id

    async def call(self, method: str, params: dict[str, JsonValue]) -> Any:
        assert activity.in_activity()
        assert not workflow.in_workflow()
        rpc_calls.append((self.plugin_id, method, params))
        if method == "plugin.describe":
            assert params == {}
            return {
                "protocol_version": 1,
                "plugin_id": self.plugin_id,
                "instructions": [
                    {"kind": "text", "text": f"static {self.plugin_id}"},
                    {"kind": "rpc", "handler": "instructions"},
                ],
                "hooks": [
                    {"id": point, "point": point, "handler": point}
                    for point in ("before_run", "before_model_request", "after_model_request")
                ],
            }
        assert method == "plugin.invoke"
        assert set(params) == {"handler", "payload"}
        if params["handler"] == "instructions":
            assert params["payload"] == {}
            return {"kind": "value", "value": f"dynamic {self.plugin_id}"}
        if params["handler"] == "before_run":
            assert params["payload"] == {}
            return {"kind": "value", "value": None}
        return {"kind": "keep"}


def client_factory(plugin_id: str):
    assert activity.in_activity()
    assert not workflow.in_workflow()
    client_lookups.append(plugin_id)
    return MemoryClient(plugin_id)


@activity.defn
async def discover(plugin_ids: list[str]) -> PluginDeps:
    return PluginDeps(manifests=await describe_plugins([MemoryClient(p) for p in plugin_ids]))


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    assert activity.in_activity()
    return ModelResponse(parts=[TextPart(info.instructions or "empty")])


host = PluginHostCapability[PluginDeps](
    lambda ctx: ctx.deps.manifests, client_factory=client_factory, id="selected_plugins"
)
durability = TemporalDurability[PluginDeps](
    activity_config={
        "start_to_close_timeout": timedelta(seconds=10),
        "retry_policy": RetryPolicy(maximum_attempts=1),
    }
)
agent = Agent(
    FunctionModel(respond),
    name="plugin_host_integration",
    deps_type=PluginDeps,
    capabilities=[host, durability],
)


@workflow.defn
class PluginWorkflow:
    @workflow.run
    async def run(self, plugin_ids: list[str]) -> str:
        deps = await workflow.execute_activity(
            discover,
            plugin_ids,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        result = await agent.run("run selected plugins", deps=deps)
        return result.output
