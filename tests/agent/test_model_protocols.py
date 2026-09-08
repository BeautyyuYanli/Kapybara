"""Exercise real SDK serialization/streaming through borrowed mock HTTP transports."""

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr

from kapy.agent import ModelConnection, create_model_backend
from kapy.state import CheckpointWrite, RunnerState, SessionInput

from .test_runner import Caller, Context, response, runner


def event(kind, **data):
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


def responses_stream(text=None, tool=None, args=None):
    item = (
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": tool,
            "arguments": json.dumps(args or {}),
            "status": "completed",
        }
        if tool
        else {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    )
    base = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "test",
        "status": "in_progress",
        "output": [],
    }
    body = event("response.created", response=base, sequence_number=0)
    body += event("response.output_item.added", output_index=0, item=item, sequence_number=1)
    if text is not None:
        body += event(
            "response.content_part.added",
            output_index=0,
            content_index=0,
            item_id="msg_1",
            part={"type": "output_text", "text": "", "annotations": []},
            sequence_number=2,
        )
        body += event(
            "response.output_text.delta",
            output_index=0,
            content_index=0,
            item_id="msg_1",
            delta=text,
            logprobs=[],
            sequence_number=3,
        )
    body += event("response.output_item.done", output_index=0, item=item, sequence_number=4)
    body += event(
        "response.completed",
        response={
            **base,
            "status": "completed",
            "output": [item],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
        sequence_number=5,
    )
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def google_stream(text=None, tool=None, args=None, *, tokens=100):
    part = (
        {"functionCall": {"name": tool, "args": args or {}}, "thoughtSignature": "c2ln"}
        if tool
        else {"text": text}
    )
    content = {
        "candidates": [
            {"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}
        ],
        "usageMetadata": {
            "promptTokenCount": tokens,
            "candidatesTokenCount": 10,
            "totalTokenCount": tokens + 10,
            "cachedContentTokenCount": 20,
        },
        "modelVersion": "test",
    }
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"data: {json.dumps(content)}\n\n",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "google_ai_studio"])
async def test_real_sdk_tool_loop_usage_and_borrowed_client(protocol):
    requests = []

    def handle(request):
        requests.append(request)
        tool = "process_list" if len(requests) == 1 else None
        text = None if tool else "Protocol complete"
        if protocol == "openai_chat":
            return response(text=text, name=tool)
        if protocol == "openai_responses":
            return responses_stream(text, tool)
        return google_stream(text, tool)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        config = ModelConnection(protocol, "https://model.invalid/base", SecretStr("dummy-key"))
        agent = runner(client, plugins=())
        agent.config = replace(agent.config, model="test")
        agent.model_backend = create_model_backend(config, client)
        ctx = Context(agent.initial_state(instructions="user instruction", skills=[]))
        result = await agent(ctx)
        assert result.output == "Protocol complete"
        assert len(requests) == 2 and not client.is_closed
        usage = result.checkpoint.state.data["last_usage"]
        assert isinstance(usage, dict) and usage["input_tokens"] == 100
        bodies = [json.loads(request.content) for request in requests]
        if protocol == "openai_responses":
            assert requests[0].url.path == "/base/responses"
            assert bodies[0]["store"] is False and "previous_response_id" not in bodies[1]
            assert any(
                item.get("type") == "function_call_output" and item["call_id"] == "call_1"
                for item in bodies[1]["input"]
            )
            call = next(item for item in bodies[1]["input"] if item.get("type") == "function_call")
            assert call["id"] == "fc_1" and call["call_id"] == "call_1"
            assert bodies[0]["truncation"] == "disabled"
        elif protocol == "google_ai_studio":
            assert requests[0].url.path == "/base/v1beta/models/test:streamGenerateContent"
            assert requests[0].headers["x-goog-api-key"] == "dummy-key"
            assert any(
                "functionResponse" in part
                for item in bodies[1]["contents"]
                for part in item["parts"]
            )
            assert any(
                part.get("thoughtSignature") == "c2ln"
                for item in bodies[1]["contents"]
                for part in item["parts"]
            )
        else:
            assert requests[0].url.path == "/base/chat/completions"
            assert any(item.get("role") == "tool" for item in bodies[1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "google_ai_studio"])
async def test_media_rejection_replays_persisted_result_without_reading_machine_twice(protocol):
    from .test_runner import Caller

    requests = []
    caller = Caller()

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            if protocol == "google_ai_studio":
                return httpx2.Response(
                    400,
                    json={
                        "error": {
                            "code": 400,
                            "message": "Unsupported image format",
                            "status": "INVALID_ARGUMENT",
                        }
                    },
                )
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported image format",
                        "type": "invalid_request_error",
                        "code": "invalid_image",
                    }
                },
            )
        tool = "read_media" if len(requests) == 1 else None
        text = None if tool else "Media was rejected"
        args = {"path": "picture.png"}
        if protocol == "openai_chat":
            return response(text=text, name=tool, args=args)
        if protocol == "openai_responses":
            return responses_stream(text, tool, args)
        return google_stream(text, tool, args)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, caller, plugins=())
        agent.model_backend = create_model_backend(
            ModelConnection(protocol, "https://model.invalid", SecretStr("dummy-key")), client
        )
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        result = await agent(ctx)
        assert result.output == "Media was rejected" and len(requests) == 3
        assert sum(method == "file.pull" for _, method, _, _ in caller.calls) == 1
        assert "iVBOR" in json.dumps(requests[1])
        assert "iVBOR" not in json.dumps(requests[2])
        assert any(
            delta.kind == "notice"
            and isinstance(delta.data, dict)
            and delta.data.get("kind") == "attempt_failed"
            for delta in ctx.deltas
        )


@pytest.mark.asyncio
async def test_model_identity_change_scrubs_only_projection_and_preserves_tool_pairs():
    import copy

    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
    )

    from kapy.agent.runner import Runtime

    from .test_runner import Caller

    async with httpx2.AsyncClient() as client:
        agent = runner(client, Caller(), plugins=())
        agent.model_identity = "provider-a:revision-1:model-a"
        ctx = Context(agent.initial_state(instructions="user", skills=[]))
        runtime = Runtime(agent, ctx, "model-a")
        await runtime.initialize()
        await runtime.record(
            ModelResponse(
                [
                    ThinkingPart(
                        "private reasoning", signature="encrypted", provider_name="openai"
                    ),
                    TextPart("public text", id="msg-private", provider_name="openai"),
                    ToolCallPart(
                        "process_list",
                        {},
                        "call-safe",
                        id="fc-private",
                        provider_name="openai",
                        provider_details={"thought_signature": "secret-signature"},
                    ),
                ],
                provider_name="openai",
                provider_response_id="resp-private",
            )
        )
        await runtime.record(
            ModelRequest([ToolReturnPart("process_list", {"items": []}, "call-safe")])
        )
        snapshot = copy.deepcopy(ctx.state)
        agent.model_identity = "provider-b:revision-2:model-b"
        restored = Runtime(agent, ctx, "model-b")
        await restored.initialize()
        encoded = json.dumps(restored.data)
        assert "private reasoning" not in encoded and "secret-signature" not in encoded
        assert "fc-private" not in encoded and "resp-private" not in encoded
        assert "public text" in encoded and encoded.count("call-safe") == 2
        assert ctx.state == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["connection", "budget"])
async def test_checkpoint_recovery_discards_stale_usage_and_scopes_signatures(
    change: str, tmp_path: Path
) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    requests: list[httpx2.Request] = []

    class MarkedCaller(Caller):
        async def call(self, *args, **kwargs):
            await super().call(*args, **kwargs)
            return {"items": [], "marker": f"restart-marker-{len(self.calls)}", "next": None}

    class InterruptedContext(Context):
        armed = False

        async def checkpoint(self, write: CheckpointWrite) -> str:
            cursor = await super().checkpoint(write)
            checkpoint_path.write_text(
                json.dumps({"state": asdict(write.state), "number": write.number})
            )
            # Stop after the actual second tool result commits, before the next
            # model boundary can consume its response's fresh API usage.
            if self.armed and "restart-marker-2" in json.dumps(write.state.data):
                raise asyncio.CancelledError
            return cursor

    def first_provider(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 2:
            return google_stream(text="previous completed answer")
        return google_stream(tool="process_list", tokens=800 if len(requests) == 3 else 100)

    caller = MarkedCaller()
    connection = ModelConnection(
        "google_ai_studio", "https://old.invalid/base", SecretStr("old-key")
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(first_provider)) as client:
        agent = runner(client, caller, plugins=(), model_identity="provider:revision-1:test")
        agent.config = replace(
            agent.config, model="test", context_window_tokens=1000, max_output_tokens=100
        )
        agent.model_backend = create_model_backend(connection, client)
        ctx = InterruptedContext(agent.initial_state(instructions="", skills=[]))
        first = await agent(ctx)
        await ctx.checkpoint(first.checkpoint)
        ctx.run_id = uuid4()
        ctx.session = replace(ctx.session, run_id=ctx.run_id)
        ctx.inputs = (SessionInput(uuid4(), 2, "queue", "continue after restart", None),)
        ctx.armed = True
        with pytest.raises(asyncio.CancelledError):
            await agent(ctx)
        assert len(requests) == 3 and len(caller.calls) == 2

    saved = json.loads(checkpoint_path.read_text())
    usage = saved["state"]["data"]["last_usage"]
    assert usage["input_tokens"] == 800 and usage["output_tokens"] == 10
    assert usage["sweep_applied"] is False
    assert saved["state"]["data"]["cycles"][0]["level"] == 0
    assert "c2ln" in json.dumps(saved)

    def recovered_provider(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return google_stream(text="restored answer")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(recovered_provider)) as client:
        restarted = runner(
            client,
            caller,
            plugins=(),
            model_identity="provider:revision-2:test"
            if change == "connection"
            else "provider:revision-1:test",
        )
        restarted.config = replace(
            agent.config, context_window_tokens=1100 if change == "budget" else 1000
        )
        restarted.model_backend = create_model_backend(
            ModelConnection("google_ai_studio", "https://new.invalid/base", SecretStr("new-key"))
            if change == "connection"
            else connection,
            client,
        )
        restored = Context(RunnerState(**saved["state"]))
        restored.session, restored.run_id = ctx.session, ctx.run_id
        restored.checkpoint_number = saved["number"]
        restored.attempt, restored.recovered, restored.inputs = 2, True, ()
        result = await restarted(restored)

    assert result.output == "restored answer" and len(requests) == 4
    assert len(caller.calls) == 2  # Both committed tools survive without reexecution.
    wire = json.loads(requests[-1].content)
    assert requests[-1].url.path.endswith("/models/test:streamGenerateContent")
    assert requests[-1].url.host == ("new.invalid" if change == "connection" else "old.invalid")
    assert requests[-1].headers["x-goog-api-key"] == (
        "new-key" if change == "connection" else "old-key"
    )
    parts = [part for message in wire["contents"] for part in message["parts"]]
    returns = [part["functionResponse"] for part in parts if "functionResponse" in part]
    assert len(returns) == 2
    assert "restart-marker-1" in json.dumps(returns)  # A stale usage sweep would omit it.
    assert "restart-marker-2" in json.dumps(returns)
    assert any(part.get("thoughtSignature") == "c2ln" for part in parts) is (change == "budget")
    assert json.loads(checkpoint_path.read_text()) == saved  # Recovery does not rewrite originals.


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", ["gpt-5", "company-reasoner"])
async def test_responses_full_replay_and_compression_preserve_reasoning_protocol_block(model_name):
    from typing import cast
    from uuid import uuid4

    from pydantic_ai import Agent
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from kapy.agent import AgentPayloadStore
    from kapy.agent.codec import MessageCodec
    from kapy.agent.compression import projection, sweep

    from .test_runner import Payloads

    messages = [
        ModelRequest([UserPromptPart("old input")]),
        ModelResponse(
            [
                ThinkingPart(
                    "", id="rs_original", signature="encrypted-state", provider_name="openai"
                ),
                ToolCallPart(
                    "process_list", {}, "call_original", id="fc_original", provider_name="openai"
                ),
            ],
            provider_name="openai",
            provider_response_id="resp_original",
        ),
        ModelRequest([ToolReturnPart("process_list", {"items": ["original"]}, "call_original")]),
        ModelResponse(
            [TextPart("old answer", id="msg_original", provider_name="openai")],
            provider_name="openai",
        ),
    ]
    codec = MessageCodec(cast(AgentPayloadStore, Payloads()), uuid4())
    data = {
        "version": 2,
        "cycles": [
            {
                "closed": True,
                "level": 0,
                "inputs": ["old input"],
                "output": "old answer",
                "messages": [await codec.encode(message) for message in messages],
            },
            {
                "closed": False,
                "level": 0,
                "messages": [await codec.encode(ModelRequest([UserPromptPart("new input")]))],
            },
        ],
    }
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["store"] is False and body["truncation"] == "disabled"
        assert "previous_response_id" not in body and "conversation" not in body
        assert "reasoning.encrypted_content" in body["include"]
        items = body["input"]
        if len(requests) <= 2:
            reasoning = next(item for item in items if item.get("type") == "reasoning")
            assert reasoning["id"] == "rs_original"
            assert reasoning["encrypted_content"] == "encrypted-state"
            following = items[items.index(reasoning) + 1]
            assert following["type"] == "function_call" and following["id"] == "fc_original"
            outputs = [item for item in items if item.get("type") == "function_call_output"]
            assert len(outputs) == 1 and outputs[0]["call_id"] == following["call_id"]
            if len(requests) == 2:
                assert "omitted" in outputs[0]["output"]
        else:
            assert all(
                item.get("type") not in {"reasoning", "function_call", "function_call_output"}
                for item in items
            )
            assert "old answer" in json.dumps(items) and "old input" in json.dumps(items)
        return responses_stream(text="replayed")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        backend = create_model_backend(
            ModelConnection("openai_responses", "https://model.invalid/v1", SecretStr("dummy")),
            client,
        )
        agent = Agent(backend.create_model(model_name))
        for level in range(3):
            # Use the same checkpoint codec used by resumed runs, then apply actual compression.
            recovered = await codec.load(await codec.state(data))
            history = [await codec.decode(message) for message in projection(recovered)]
            async with agent.run_stream(None, message_history=history) as result:
                assert await result.get_output() == "replayed"
            if level < 2:
                assert sweep(data, 0.10)
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "google_ai_studio"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_session_output_functions_across_model_backends(protocol, explicit):
    from kapy.state import ReplyTo, WaitFor

    address = uuid4()
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        text = "Complete reply" if explicit and len(requests) == 1 else None
        tool = None if text else "reply_to" if explicit else "wait_for"
        args = {"ids": [str(address)]}
        if protocol == "openai_chat":
            return response(text=text, name=tool, args=args)
        if protocol == "openai_responses":
            return responses_stream(text, tool, args)
        return google_stream(text, tool, args)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, plugins=())
        agent.config = replace(agent.config, model="test")
        agent.model_backend = create_model_backend(
            ModelConnection(protocol, "https://model.invalid/base", SecretStr("dummy")), client
        )
        ctx = Context(agent.initial_state(instructions="Follow user instructions", skills=[]))
        ctx.session = replace(
            ctx.session, config={"output_mode": "reply_to" if explicit else "text"}
        )
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        result = await agent(ctx)
    assert result.output == (
        ReplyTo((address,), "Complete reply") if explicit else WaitFor((address,))
    )
    assert len(requests) == (2 if explicit else 1)
    assert "history(seq bigint NOT NULL" in json.dumps(requests[0])
    assert ("being_waited_id" in json.dumps(requests[0])) == explicit
