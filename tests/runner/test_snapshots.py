"""Real SDK runs resume serialized model/tool boundaries without another prompt."""

from copy import deepcopy

import pytest
from pydantic_ai import (
    Agent,
    CallToolsNode,
    ModelRequestNode,
    RunContext,
    TextOutput,
    ToolOutput,
    UserPromptNode,
)
from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import ModelRetry, ToolFailed
from pydantic_ai.messages import (
    INTERRUPTED_TOOL_RETURN_CONTENT,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from kapy.agent_history import HistoryRepository
from kapy.control.sessions.models import SessionRow
from kapy.layers.registry import layer_registry
from kapy.layers.shell.models import Config, State
from kapy.runner import Recorder, RunnerDeps, RunnerPersistenceCapability
from kapy.runner.output import handle_deltas
from pydantic_ai_layer_protocol import dump_object

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

INTERRUPTION_PROMPT = (
    "Unexpected interruption. Your messages may or may not be delivered. "
    "If your messages were intended to make changes, "
    "retry only if the operation is idempotent; otherwise, "
    "verify the outcome before taking further action."
)


def model_with_stream(respond):
    """Exercise identical deterministic responses through both public SDK model paths."""

    async def stream_response(messages, info):
        response = await respond(messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args_as_json_str(),
                        tool_call_id=part.tool_call_id,
                    )
                }

    return FunctionModel(respond, stream_function=stream_response)


class BoundaryReached(Exception):
    """Stop before the next node, leaving the preceding boundary in storage."""


@pytest.mark.parametrize("boundary", ["model", "tool"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_interrupted_snapshot_resumes_without_prompt(runner_database, boundary, streaming):
    deps = runner_database.deps()
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, deps.session_id)
        row.layers = {"shell": dump_object(Config(cwd="/tmp"))}
        row.layer_states = {"shell": dump_object(State())}
        deps.layers = layer_registry.load(row.layers, row.layer_states)

    calls = []
    requests = []
    live_messages = []
    interrupt = True

    def update_state(deps: RunnerDeps, key: str) -> None:
        state = State.model_validate(deps.layers["shell"]["state"])
        state.jobs[key] = 1
        deps.layers["shell"]["state"] = dump_object(state)

    async def pause(ctx, *, node):
        at_model_boundary = boundary == "model" and isinstance(node, CallToolsNode)
        at_tool_boundary = (
            boundary == "tool"
            and isinstance(node, ModelRequestNode)
            and any(isinstance(part, ToolReturnPart) for part in node.request.parts)
        )
        if interrupt and (at_model_boundary or at_tool_boundary):
            live_messages.extend(deepcopy(ctx.messages))
            if isinstance(node, ModelRequestNode):
                live_messages.append(deepcopy(node.request))
            raise BoundaryReached
        return node

    async def finalize_node(ctx, *, node, result):
        if isinstance(node, ModelRequestNode):
            ctx.messages[-1].metadata = {**(ctx.messages[-1].metadata or {}), "finalized": True}
            update_state(ctx.deps, "model_hook")
        elif isinstance(node, CallToolsNode) and isinstance(result, ModelRequestNode):
            result.request.metadata = {"finalized": True}
            update_state(ctx.deps, "tool_hook")
        return result

    async def before_request(ctx, request_context):
        requests.append(deepcopy(list(request_context.messages)))
        return request_context

    async def respond(messages, info):
        if any(isinstance(message, ModelResponse) for message in messages):
            return ModelResponse([TextPart("done")])
        return ModelResponse([ToolCallPart("work", {}, f"work-{index}") for index in range(2)])

    recorder = Recorder()
    agent = Agent(
        model_with_stream(respond),
        deps_type=RunnerDeps,
        capabilities=[
            RunnerPersistenceCapability(recorder),
            Hooks(
                before_node_run=pause,
                after_node_run=finalize_node,
                before_model_request=before_request,
            ),
        ],
    )

    @agent.tool
    async def work(ctx: RunContext[RunnerDeps]) -> str:
        calls.append(ctx.deps.session_id)
        update_state(ctx.deps, "work")
        return "tool result"

    with pytest.raises(BoundaryReached):
        await agent.run("go", deps=deps, event_stream_handler=handle_deltas if streaming else None)

    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, deps.session_id)
        assert row.runner_state is not None
        messages = ModelMessagesTypeAdapter.validate_json(row.runner_state)
        restored_deps = runner_database.deps()
        restored_deps.layers = layer_registry.load(row.layers, row.layer_states)
        entries = await HistoryRepository(db).read_history_entries(deps.session_id)

    assert all(message.state == "complete" for message in live_messages)
    assert [message.state for message in messages] == [
        *("complete" for _ in messages[:-1]),
        "interrupted",
    ]
    assert messages[-2].metadata == {"finalized": True, "seq": 1}
    assert [entry.seq for entry in entries] == [0, 1]
    assert all(entry.message.state == "complete" for entry in entries)
    saved_state = State.model_validate(restored_deps.layers["shell"]["state"])
    assert isinstance(messages[-1], ModelRequest)
    if boundary == "model":
        assert messages[-1].metadata is None and len(messages[-1].parts) == 1
        retry = messages[-1].parts[0]
        assert isinstance(retry, RetryPromptPart) and retry.content == INTERRUPTION_PROMPT
        assert retry.tool_name is None
        assert calls == []
        assert saved_state.jobs == {"model_hook": 1}
    else:
        assert messages[-1].metadata == {"finalized": True}
        assert len(messages[-1].parts) == 2
        assert isinstance(messages[-1].parts[0], ToolReturnPart)
        assert messages[-1].parts[0].content == "tool result"
        assert saved_state.jobs == {"model_hook": 1, "work": 1, "tool_hook": 1}

    interrupt = False
    result = await agent.run(
        deps=restored_deps,
        message_history=messages,
        event_stream_handler=handle_deltas if streaming else None,
    )
    assert result.output == "done"
    assert calls == ([] if boundary == "model" else [deps.session_id, deps.session_id])
    assert len(requests) == 2
    response = requests[-1][-2]
    assert isinstance(response, ModelResponse)
    request = requests[-1][-1]
    assert isinstance(request, ModelRequest) and request.state == "complete"
    returns = [part for part in request.parts if isinstance(part, ToolReturnPart)]
    assert len(returns) == len(response.tool_calls) == 2
    for part, call in zip(returns, response.tool_calls, strict=True):
        assert isinstance(part, ToolReturnPart)
        assert (part.tool_name, part.tool_call_id) == (call.tool_name, call.tool_call_id)
        assert part.outcome == ("interrupted" if boundary == "model" else "success")
        assert part.content == (
            INTERRUPTED_TOOL_RETURN_CONTENT if boundary == "model" else "tool result"
        )
    retries = [part for part in request.parts if isinstance(part, RetryPromptPart)]
    assert [part.content for part in retries] == (
        [INTERRUPTION_PROMPT] if boundary == "model" else []
    )
    assert result.all_messages()[-1].state == "complete"
    assert [
        part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ] == ["go"]
    async with runner_database.sessions.begin() as db:
        entries = await HistoryRepository(db).read_history_entries(deps.session_id)
    assert [entry.seq for entry in entries] == [0, 1, 2, 3]


@pytest.mark.parametrize("output_mode", ["text", "text_function", "tool_output"])
@pytest.mark.parametrize("instructions", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_end_retains_model_snapshot_and_resumes_with_new_response(
    runner_database, output_mode, instructions, streaming
):
    deps = runner_database.deps()
    requests = []
    processed = []

    def finish(text: str) -> str:
        processed.append(text)
        return "processed:" + text

    if output_mode == "text_function":
        output_type = TextOutput(finish)
    elif output_mode == "tool_output":
        output_type = ToolOutput(finish, name="finish")
    else:
        output_type = str

    async def before_request(ctx, request_context):
        requests.append(deepcopy(list(request_context.messages)))
        return request_context

    async def respond(messages, info):
        text = f"response-{len(requests)}"
        if output_mode == "tool_output":
            return ModelResponse(
                [ToolCallPart("finish", {"text": text}, f"finish-{len(requests)}")]
            )
        return ModelResponse([TextPart(text)])

    agent = Agent(
        model_with_stream(respond),
        output_type=output_type,
        deps_type=RunnerDeps,
        instructions="Continue the conversation." if instructions else None,
        capabilities=[
            RunnerPersistenceCapability(Recorder()),
            Hooks(before_model_request=before_request),
        ],
    )

    async with agent.iter("go", deps=deps) as run:
        node = run.next_node
        assert isinstance(node, UserPromptNode)
        node = await run.next(node)
        assert isinstance(node, ModelRequestNode)
        node = await run.next(node)
        assert isinstance(node, CallToolsNode) and processed == []
        async with runner_database.sessions.begin() as db:
            row = await db.get(SessionRow, deps.session_id)
            model_snapshot = row.runner_state
        assert model_snapshot is not None
        node = await run.next(node)
        assert Agent.is_end_node(node)
        live_messages = deepcopy(run.all_messages())

    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, deps.session_id)
        assert row.runner_state == model_snapshot
    messages = ModelMessagesTypeAdapter.validate_json(model_snapshot)
    assert all(message.state == "complete" for message in live_messages)
    assert not any(isinstance(part, RetryPromptPart) for m in live_messages for part in m.parts)
    assert isinstance(messages[-1], ModelRequest) and messages[-1].state == "interrupted"
    assert len(messages[-1].parts) == 1
    retry = messages[-1].parts[0]
    assert isinstance(retry, RetryPromptPart) and retry.content == INTERRUPTION_PROMPT
    assert processed == ([] if output_mode == "text" else ["response-1"])

    assert len(requests) == 1
    result = await agent.run(
        deps=runner_database.deps(),
        message_history=messages,
        event_stream_handler=handle_deltas if streaming else None,
    )
    assert result.output == ("response-2" if output_mode == "text" else "processed:response-2")
    assert len(requests) == 2
    assert processed == ([] if output_mode == "text" else ["response-1", "response-2"])
    parts = requests[-1][-1].parts
    assert [p.content for p in parts if isinstance(p, RetryPromptPart)] == [INTERRUPTION_PROMPT]
    returns = [p for p in parts if isinstance(p, ToolReturnPart)]
    assert len(returns) == (1 if output_mode == "tool_output" else 0)
    assert all(p.outcome == "interrupted" for p in returns)


@pytest.mark.parametrize("retry_hook", ["before", "after", "output"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_model_retry_snapshot_preserves_retry_prompt(runner_database, retry_hook, streaming):
    retried = False
    interrupt = True
    requests = []

    def request_retry(hook):
        nonlocal retried
        if hook == retry_hook and not retried:
            retried = True
            raise ModelRetry("Use the corrected instructions.")

    async def before_request(ctx, request_context):
        request_retry("before")
        return request_context

    async def after_request(ctx, *, request_context, response):
        request_retry("after")
        return response

    async def pause(ctx, *, node):
        if interrupt and isinstance(node, ModelRequestNode):
            if any(isinstance(part, RetryPromptPart) for part in node.request.parts):
                raise BoundaryReached
        return node

    async def respond(messages, info):
        requests.append(deepcopy(messages))
        return ModelResponse([TextPart("done")])

    def finish(text: str) -> str:
        request_retry("output")
        return text

    agent = Agent(
        model_with_stream(respond),
        output_type=TextOutput(finish) if retry_hook == "output" else str,
        deps_type=RunnerDeps,
        capabilities=[
            RunnerPersistenceCapability(Recorder()),
            Hooks(
                before_node_run=pause,
                before_model_request=before_request,
                after_model_request=after_request,
            ),
        ],
    )
    with pytest.raises(BoundaryReached):
        await agent.run(
            "go",
            deps=runner_database.deps(),
            event_stream_handler=handle_deltas if streaming else None,
        )

    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, runner_database.session_id)
        assert row.runner_state is not None
        messages = ModelMessagesTypeAdapter.validate_json(row.runner_state)
    assert isinstance(messages[-1], ModelRequest) and messages[-1].state == "interrupted"
    assert len(messages[-1].parts) == 1
    retry = messages[-1].parts[0]
    assert isinstance(retry, RetryPromptPart) and retry.content == "Use the corrected instructions."

    interrupt = False
    result = await agent.run(
        deps=runner_database.deps(),
        message_history=messages,
        event_stream_handler=handle_deltas if streaming else None,
    )
    assert result.output == "done"
    assert any(
        isinstance(part, RetryPromptPart) and part.content == "Use the corrected instructions."
        for part in requests[-1][-1].parts
    )


@pytest.mark.parametrize("signal", [ModelRetry, ToolFailed])
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_feedback_snapshot_preserves_retry_or_failure(
    runner_database, signal, streaming
):
    interrupt = True
    calls = []
    requests = []

    async def respond(messages, info):
        requests.append(deepcopy(messages))
        if any(isinstance(message, ModelResponse) for message in messages):
            return ModelResponse([TextPart("done")])
        return ModelResponse([ToolCallPart("work", {}, "work-call")])

    async def pause(ctx, *, node):
        if interrupt and isinstance(node, ModelRequestNode):
            if any(
                isinstance(part, (RetryPromptPart, ToolReturnPart)) and part.tool_name == "work"
                for part in node.request.parts
            ):
                raise BoundaryReached
        return node

    agent = Agent(
        model_with_stream(respond),
        deps_type=RunnerDeps,
        capabilities=[RunnerPersistenceCapability(Recorder()), Hooks(before_node_run=pause)],
    )

    @agent.tool_plain
    async def work() -> str:
        calls.append("work")
        raise signal("tool unavailable")

    with pytest.raises(BoundaryReached):
        await agent.run(
            "go",
            deps=runner_database.deps(),
            event_stream_handler=handle_deltas if streaming else None,
        )
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, runner_database.session_id)
        assert row.runner_state is not None
        messages = ModelMessagesTypeAdapter.validate_json(row.runner_state)
    request = messages[-1]
    assert isinstance(request, ModelRequest) and request.state == "interrupted"
    assert len(request.parts) == 1
    feedback = request.parts[0]
    if signal is ModelRetry:
        assert isinstance(feedback, RetryPromptPart)
    else:
        assert isinstance(feedback, ToolReturnPart) and feedback.outcome == "failed"
    assert (feedback.tool_name, feedback.tool_call_id, feedback.content) == (
        "work",
        "work-call",
        "tool unavailable",
    )

    interrupt = False
    result = await agent.run(
        deps=runner_database.deps(),
        message_history=messages,
        event_stream_handler=handle_deltas if streaming else None,
    )
    assert result.output == "done" and calls == ["work"]
    assert requests[-1][-1].parts == [feedback]
