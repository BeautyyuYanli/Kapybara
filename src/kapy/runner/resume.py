"""Select an explicit saved continuation after native SDK run initialization."""

from copy import deepcopy

from pydantic_ai import CallToolsNode, ModelRequestNode, RunContext, UserPromptNode
from pydantic_ai.capabilities import AgentNode, NodeResult
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.result import FinalResult
from pydantic_graph import End

from .checkpoints import CallToolsNext, FinishNext, ModelRequestNext
from .recording import MessageRecordCapability
from .types import RunnerDeps


class ResumeCapability(MessageRecordCapability):
    """Run-local native capability; saved nodes bypass their completed work/hooks."""

    def __init__(self) -> None:
        self.check_tools = False
        self.pending_messages: list[ModelMessage] = []

    async def after_node_run(
        self,
        ctx: RunContext[RunnerDeps],
        *,
        node: AgentNode[RunnerDeps],
        result: NodeResult[RunnerDeps],
    ) -> NodeResult[RunnerDeps]:
        if not isinstance(node, UserPromptNode):
            return result
        saved = ctx.deps.resume
        assert saved is not None
        ctx.deps.recorder.validate_messages(ctx.messages)
        appended = self.pending_messages + deepcopy(ctx.messages[len(saved.messages) :])
        self.pending_messages = []
        messages = ctx.deps.messages
        messages[:] = deepcopy(saved.messages)
        if isinstance(saved.next, ModelRequestNext):
            request = messages.pop()
            assert isinstance(request, ModelRequest)
            # before_run additions follow the saved request once SDK preparation
            # appends it. They cannot displace a frozen record or be discarded.
            self.pending_messages = appended
            return ModelRequestNode(request, is_resuming_without_prompt=True)
        messages.extend(appended)
        if isinstance(saved.next, CallToolsNext):
            response = messages[len(saved.messages) - 1]
            assert isinstance(response, ModelResponse)
            self.check_tools = True
            return CallToolsNode(response)
        if isinstance(saved.next, FinishNext):
            return End(
                FinalResult(
                    saved.next.output,
                    tool_name=saved.next.tool_name,
                    tool_call_id=saved.next.tool_call_id,
                )
            )
        raise ValueError("Completed checkpoints must return from admission without SDK resources")

    async def before_model_request(
        self, ctx: RunContext[RunnerDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if self.pending_messages:
            request_context.messages = [*request_context.messages, *self.pending_messages]
            ctx.messages.extend(self.pending_messages)
            self.pending_messages = []
        return request_context

    async def before_node_run(
        self, ctx: RunContext[RunnerDeps], *, node: AgentNode[RunnerDeps]
    ) -> AgentNode[RunnerDeps]:
        if isinstance(node, UserPromptNode):
            saved = ctx.deps.resume
            assert saved is not None
            ctx.deps.recorder.validate_messages(ctx.messages)
            self.pending_messages = deepcopy(ctx.messages[len(saved.messages) :])
            # Initialization must see the saved frontier, so response resumption
            # prepares its tool manager. Reattach hook additions after that node.
            del ctx.messages[len(saved.messages) :]
        if self.check_tools and isinstance(node, CallToolsNode):
            # Validate the entire pending batch against current definitions before
            # any tool runs. SDK retry/synthetic error receipts cannot mask drift.
            manager = ctx.tool_manager
            if manager is None or manager.tools is None:
                raise ValueError("Restored tools are not available")
            for call in node.model_response.tool_calls:
                tool = manager.tools.get(call.tool_name)
                if tool is None:
                    raise ValueError("Restored tool is no longer registered")
                if isinstance(call.args, str):
                    tool.args_validator.validate_json(call.args)
                else:
                    tool.args_validator.validate_python(call.args or {})
            self.check_tools = False
        return node
