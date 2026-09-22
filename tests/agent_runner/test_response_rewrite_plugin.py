"""Rewriter boundaries using the real OpenAI HTTP adapter and native SDK runner."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import anyio
import httpx2
import pytest
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, PromptedOutput, RunContext
from pydantic_ai.messages import (
    BinaryContent,
    FilePart,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartStartEvent,
    SpeechPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters, OutputMode
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage

from kapy.agent_plugins import AgentPluginService, PluginSpec
from kapy.agent_plugins.builtin import response_rewrite as rewrite
from kapy.agent_plugins.contracts import SessionContext
from kapy.agent_runner.auxiliary import run_auxiliary
from kapy.agent_runner.repository import AgentRepository
from kapy.application.agent import create_execution_factory, create_registry
from kapy.context_plugins.summary import COMPACTION_PROMPT
from kapy.control.sessions import CreateSession, SessionService
from kapy.lifecycle import LifecycleStatus

CONFIG = {
    "prompt": "  Rewrite {answer} literally.\nKeep meaning.  ",
    "base_url": "https://rewrite.example/v1/",
    "api_key": "rewrite-secret",
}


class UnusedState:
    async def read(self):
        raise AssertionError("stateless plugin read state")

    async def replace(self, value, *, expected_revision):
        raise AssertionError("stateless plugin wrote state")


def completion(content="rewritten", **message_fields):
    return {
        "id": "rewrite-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gemini-3.8-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, **message_fields},
            }
        ],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 2000, "total_tokens": 3000},
    }


@pytest.fixture
def rewrite_http(monkeypatch):
    clients, requests = [], []
    responses: list[
        dict
        | Exception
        | httpx2.Response
        | Callable[[httpx2.Request], Awaitable[dict | httpx2.Response]]
    ] = []

    async def respond(request):
        requests.append(request)
        assert responses, "unexpected rewriter HTTP request"
        response = responses.pop(0)
        if callable(response):
            response = await response(request)
        if isinstance(response, Exception):
            raise response
        return (
            response
            if isinstance(response, httpx2.Response)
            else httpx2.Response(200, json=response)
        )

    def create(**kwargs):
        client = AsyncOpenAI(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(rewrite, "AsyncOpenAI", create)
    monkeypatch.setattr(
        rewrite,
        "DefaultAsyncHttpxClient",
        lambda **kwargs: DefaultAsyncHttpxClient(transport=httpx2.MockTransport(respond), **kwargs),
    )
    context = SessionContext(
        uuid4(),
        "builtin",
        "response_rewrite",
        rewrite.ResponseRewriteConfig(**CONFIG),
        UnusedState(),
    )
    return context, clients, requests, responses


async def apply(binding, response, *, output_mode: OutputMode = "text", metadata=None):
    model = FunctionModel(lambda messages, info: response)
    return await binding.capabilities[0].after_model_request(
        RunContext(deps=None, model=model, usage=RunUsage(), metadata=metadata),
        request_context=ModelRequestContext(
            model=model,
            messages=[ModelRequest(parts=[UserPromptPart("private history")])],
            model_settings={"temperature": 0.75},
            model_request_parameters=ModelRequestParameters(output_mode=output_mode),
        ),
        response=response,
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"prompt": ""},
        {"prompt": " \n"},
        {"prompt": 3},
        {"api_key": ""},
        {"api_key": "\t"},
        {"base_url": "ftp://rewrite.example/v1"},
        {"base_url": "relative"},
        {"base_url": "https://user:secret@rewrite.example/v1"},
        {"base_url": "https://rewrite.example/v1?key=secret"},
        {"base_url": "https://rewrite.example/v1#fragment"},
        {"unknown": True},
    ],
)
def test_invalid_config_is_rejected(invalid):
    with pytest.raises(ValidationError):
        rewrite.ResponseRewriteConfig.model_validate(CONFIG | invalid)


def test_config_roundtrip_hides_key_only_in_repr():
    config = rewrite.ResponseRewriteConfig.model_validate(CONFIG)
    assert CONFIG["api_key"] not in repr(config)
    assert config.model_dump(mode="json", round_trip=True) == CONFIG
    assert rewrite.ResponseRewriteConfig.model_validate_json(config.model_dump_json()) == config
    for missing in CONFIG:
        with pytest.raises(ValidationError):
            rewrite.ResponseRewriteConfig.model_validate(
                {key: value for key, value in CONFIG.items() if key != missing}
            )


@pytest.mark.asyncio
async def test_real_sdk_request_replaces_text_preserving_thinking_and_response(rewrite_http):
    context, clients, requests, responses = rewrite_http
    original = ModelResponse(
        parts=[
            ThinkingPart("hidden first", signature="signature"),
            TextPart("first", id="old-id", provider_details={"old": True}),
            TextPart(" second"),
            ThinkingPart("hidden middle"),
            TextPart("third"),
            ThinkingPart("hidden last"),
        ],
        usage=RequestUsage(input_tokens=12, output_tokens=3),
        model_name="business-model",
        provider_name="business-provider",
        provider_response_id="business-id",
        provider_details={"detail": "retained"},
        metadata={"opaque": 1},
        run_id="run-id",
        conversation_id="conversation-id",
        finish_reason="stop",
    )
    before = deepcopy(original)
    responses.append(completion("  new answer\n"))
    async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
        result = await apply(binding, original)
        assert clients[0].max_retries == 0
    assert original == before
    assert result is not original and replace(result, parts=original.parts) == original
    assert result.parts == [
        original.parts[0],
        TextPart("  new answer\n"),
        original.parts[3],
        original.parts[5],
    ]
    assert len(requests) == 1 and requests[0].method == "POST"
    assert str(requests[0].url) == "https://rewrite.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer rewrite-secret"
    assert json.loads(requests[0].content) == {
        "model": "gemini-3.8-flash",
        "stream": False,
        "messages": [
            {"role": "system", "content": CONFIG["prompt"]},
            {"role": "user", "content": "first second\n\nthird"},
        ],
    }
    assert clients[0].is_closed()
    await rewrite.ResponseRewritePlugin().close_session(context)


@pytest.mark.asyncio
async def test_gemini_text_with_thought_signature_is_rewritten(rewrite_http):
    context, clients, requests, responses = rewrite_http
    # Google's Chat Completions contract puts the signature on plain-text messages:
    # https://github.com/google/adk-java/blob/main/core/src/main/java/com/google/adk/models/chat/ChatCompletionsResponse.java
    responses.append(
        completion(extra_content={"google": {"thought_signature": "opaque-signature"}})
    )
    async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
        agent = Agent(
            FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("original")])),
            capabilities=binding.capabilities,
        )
        result = await agent.run("answer")
    final = result.all_messages()[-1]
    assert isinstance(final, ModelResponse)
    assert result.output == "rewritten" and final.parts == [TextPart("rewritten")]
    assert len(requests) == 1 and clients[0].is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,mode,metadata",
    [
        (ModelResponse(parts=[]), "text", None),
        (ModelResponse(parts=[ThinkingPart("secret")]), "text", None),
        (ModelResponse(parts=[TextPart(" \n")]), "text", None),
        *[
            (ModelResponse(parts=[TextPart("original")], state=state), "text", None)
            for state in ("incomplete", "suspended", "interrupted")
        ],
        *[
            (ModelResponse(parts=[TextPart("original")], finish_reason=reason), "text", None)
            for reason in ("length", "content_filter", "tool_call", "error")
        ],
        *[
            (ModelResponse(parts=[TextPart("original"), part]), "text", None)
            for part in (
                ToolCallPart("work", {}),
                NativeToolCallPart("web_search", {}),
                NativeToolReturnPart("web_search", "found"),
                FilePart(BinaryContent(data=b"image", media_type="image/png")),
                FilePart(BinaryContent(data=b"audio", media_type="audio/wav")),
                SpeechPart(speaker="assistant", transcript="spoken text"),
            )
        ],
        *[
            (ModelResponse(parts=[TextPart("original")]), mode, None)
            for mode in ("tool", "native", "prompted")
        ],
        (ModelResponse(parts=[TextPart("original")]), "text", {"kapy_run_kind": "auxiliary"}),
    ],
)
async def test_nonfinal_or_nontext_responses_make_no_request(
    rewrite_http, response, mode, metadata
):
    context, clients, requests, _ = rewrite_http
    async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
        assert await apply(binding, response, output_mode=mode, metadata=metadata) is response
    assert not requests and clients[0].is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote",
    [
        httpx2.Response(307, headers={"location": "https://rewrite.example/v1/redirect"}),
        httpx2.Response(429, json={"error": {"message": "remote-secret"}}),
        httpx2.Response(500, text="remote-secret"),
        httpx2.Response(200, content=b"not-json-remote-secret"),
        httpx2.ConnectError("remote-secret"),
        httpx2.ReadTimeout("remote-secret"),
        {},
        {"choices": []},
        completion(""),
        completion(" \n"),
        completion(None),
        completion([{"type": "text", "text": "wrong-shape"}]),
        completion(refusal="remote-secret"),
        completion(
            tool_calls=[
                {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ]
        ),
        completion(function_call={"name": "x", "arguments": "{}"}),
        completion(audio={"id": "a", "data": "x", "expires_at": 1, "transcript": "audio"}),
        completion(images=[{"image_url": "https://media.example/a.png"}]),
        completion(
            extra_content={"google": {"thought_signature": "opaque-signature"}},
            images=[{"image_url": "https://media.example/a.png"}],
        ),
        completion(extra_content={"google": {"thought_signature": "opaque", "image": "media"}}),
        completion(extra_content={"google": {"thought_signature": {"image": "media"}}}),
        completion()
        | {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "cut short"},
                }
            ]
        },
    ],
)
async def test_failure_returns_original_without_retry_or_sensitive_log(
    rewrite_http, caplog, remote
):
    context, clients, requests, responses = rewrite_http
    responses.append(remote)
    original = ModelResponse(parts=[TextPart("original-private-answer")])
    async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
        assert await apply(binding, original) is original
    assert len(requests) == 1 and clients[0].is_closed()
    records = [record for record in caplog.records if record.name == rewrite.__name__]
    assert len(records) == 1 and "Response rewrite failed (" in records[0].getMessage()
    for secret in (*CONFIG.values(), "original-private-answer", "remote-secret"):
        assert secret not in records[0].getMessage()


@pytest.mark.asyncio
async def test_total_deadline_preserves_original(rewrite_http, monkeypatch):
    context, clients, requests, responses = rewrite_http
    monkeypatch.setattr(rewrite, "REWRITE_TIMEOUT_SECONDS", 0.02)

    async def blocked(request):
        await asyncio.Event().wait()

    responses.append(blocked)
    original = ModelResponse(parts=[TextPart("original")])
    async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
        async with asyncio.timeout(1):
            assert await apply(binding, original) is original
    assert len(requests) == 1 and clients[0].is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancellation_propagates_and_closes_client(
    rewrite_http, cancellation, caplog, monkeypatch
):
    context, clients, requests, responses = rewrite_http
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()

    async def run():
        async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
            close = clients[0].close

            async def observed_close():
                await anyio.lowlevel.checkpoint()
                await close()
                cleaned.set()

            monkeypatch.setattr(clients[0], "close", observed_close)
            await apply(binding, ModelResponse(parts=[TextPart("original")]))
            pytest.fail("cancellation was swallowed")

    responses.append(blocked)
    if cancellation == "asyncio":
        task = asyncio.create_task(run())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(run)
            await asyncio.wait_for(entered.wait(), 5)
            group.cancel_scope.cancel()
    assert len(requests) == 1 and clients[0].is_closed() and cleaned.is_set()
    assert not [record for record in caplog.records if record.name == rewrite.__name__]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", ["error", "timeout"])
@pytest.mark.parametrize("execution_failure", [None, "error", "cancellation"])
async def test_cleanup_failure_propagates_with_active_execution_error(
    rewrite_http, monkeypatch, cleanup_failure, execution_failure
):
    context, _, _, _ = rewrite_http
    monkeypatch.setattr(rewrite, "CLEANUP_TIMEOUT_SECONDS", 0.02)
    cleanup_finished = asyncio.Event()
    original = (
        ValueError("execution failed")
        if execution_failure == "error"
        else asyncio.CancelledError("execution cancelled")
        if execution_failure == "cancellation"
        else None
    )

    class FailingCloseTransport(httpx2.MockTransport):
        async def aclose(self):
            try:
                await anyio.lowlevel.checkpoint()
                if cleanup_failure == "timeout":
                    await asyncio.Event().wait()
                raise OSError("transport close failed")
            finally:
                cleanup_finished.set()

    monkeypatch.setattr(
        rewrite,
        "DefaultAsyncHttpxClient",
        lambda **kwargs: DefaultAsyncHttpxClient(
            transport=FailingCloseTransport(lambda request: httpx2.Response(200)), **kwargs
        ),
    )
    expected = TimeoutError if cleanup_failure == "timeout" else OSError
    with pytest.raises(expected) as caught:
        async with rewrite.ResponseRewritePlugin().open_execution(context):
            if original is not None:
                raise original
    assert cleanup_finished.is_set()
    if original is not None:
        chain = []
        current = caught.value
        while current is not None:
            chain.append(current)
            current = current.__context__
        assert original in chain


@pytest.mark.asyncio
async def test_native_stream_previews_original_and_returns_rewrite(rewrite_http):
    context, clients, requests, responses = rewrite_http
    preview = []

    async def model(messages, info):
        yield "original "
        yield "answer"

    async def events(ctx, stream):
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                preview.append(event.part.content)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                preview.append(event.delta.content_delta)

    responses.append(completion())
    async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
        agent = Agent(FunctionModel(stream_function=model), capabilities=binding.capabilities)
        result = await agent.run("answer", event_stream_handler=events)
    assert "".join(preview) == "original answer"
    final = result.all_messages()[-1]
    assert isinstance(final, ModelResponse)
    assert result.output == "rewritten" and final.text == "rewritten"
    assert len(requests) == 1 and clients[0].is_closed()


@pytest.mark.asyncio
async def test_native_auxiliary_and_structured_runs_skip_rewriting(rewrite_http):
    context, clients, requests, _ = rewrite_http

    class Result(BaseModel):
        count: int

    async with rewrite.ResponseRewritePlugin().open_execution(context) as binding:
        agent = Agent(
            FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("summary")]))
        )
        assert (
            await run_auxiliary(
                agent,
                "any prompt",
                history=[],
                session_id=uuid4(),
                deps=None,
                capabilities=binding.capabilities,
            )
            == "summary"
        )
        structured = Agent(
            FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart('{"count":3}')])),
            output_type=PromptedOutput(Result),
            capabilities=binding.capabilities,
        )
        assert (await structured.run("produce JSON")).output == Result(count=3)
    assert not requests and clients[0].is_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_registered_plugin_persists_rewrite_before_checkpoint_and_skips_compaction(
    database, seed_session, session_model, rewrite_http
):
    _, clients, requests, responses = rewrite_http
    plugins = AgentPluginService(database.sessions, create_registry())
    service = SessionService(
        database.sessions,
        plugin_service=plugins,
        execution_factory=create_execution_factory(plugins),
    )
    template_id = uuid4()
    await seed_session(template_id)
    template = await service.get_session(template_id)
    session = await service.create_session(
        CreateSession(
            provider_id=template.provider_id,
            model_name=template.model_name,
            plugins=[
                PluginSpec(plugin_provider="builtin", plugin_name="response_rewrite", config=CONFIG)
            ],
            compaction_threshold_tokens=1,
        )
    )
    before = (await plugins.list_bindings(session.id))[0]
    assert before.config == CONFIG and before.state is None and before.data_version == 1
    model_calls = []

    def model(messages, info):
        model_calls.append(deepcopy(messages))
        summary = messages[-1].parts[-1].content == COMPACTION_PROMPT
        return ModelResponse(
            parts=[TextPart("summary" if summary else "original")],
            usage=RequestUsage(input_tokens=12, output_tokens=3),
        )

    async def rewritten(request):
        history = (await service.read_history(session.id)).items
        assert len(history) == 2 * len(requests) - 1
        assert isinstance(history[-1].message, ModelRequest)
        return completion()

    session_model(FunctionModel(model))
    responses.extend([rewritten, rewritten])
    await service.enqueue_input(session.id, "steer", "first")
    await service.enqueue_input(session.id, "queued", "second")
    assert (await service.start_runner(session.id)).output == "rewritten"
    assert len(requests) == 2 and len(model_calls) == 4
    assert len(clients) == 1 and clients[0].is_closed()
    saved = (await service.read_history(session.id)).items
    assert [item.message.text for item in saved if isinstance(item.message, ModelResponse)] == [
        "rewritten",
        "rewritten",
    ]
    for item in saved[1::2]:
        assert isinstance(item.message, ModelResponse)
        assert item.message.usage == RequestUsage(input_tokens=12, output_tokens=3)
    async with database.sessions.begin() as db:
        page = await AgentRepository(db).read_latest_page(session.id)
        assert page is not None and page.payload == {"summary": "summary"}
    assert (await service.start_runner(session.id)).output is None
    assert len(requests) == 2
    after = (await plugins.list_bindings(session.id))[0]
    assert (
        after.config == before.config and after.state is None and after.revision == before.revision
    )
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
