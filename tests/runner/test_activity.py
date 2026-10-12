"""Real standalone Activity execution, local model protocols and storage lifecycles.

Each test owns its PostgreSQL schema, Activity identity and queue. Model requests
use deterministic local HTTP endpoints; no external model API is called.
"""

import asyncio
import json
import os
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from threading import Thread
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import CapabilityOrdering, Hooks
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.settings import ModelSettings
from sqlmodel import col, select
from temporalio import activity
from temporalio.client import ActivityExecutionStatus, ActivityFailureError, Client
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.worker import Worker

from kapy.agent_history import HistoryRepository
from kapy.agent_history.models import AgentHistoryRow
from kapy.agent_output import MessageCommitted, OutputEvent, TextDelta
from kapy.application.resources import connect_temporal
from kapy.control.models.runtime import build_model, build_provider, resolve_classes
from kapy.control.sessions.repository import SessionRepository
from kapy.runner import (
    ExecutionConfig,
    MessageRecordCapability,
    RunnerActivity,
    RunnerDeps,
    RunnerInput,
    agent,
)
from kapy.runner.recording import Recorder
from kapy.runner.worker import serve


async def start(client, queue, data, **kwargs):
    return await client.start_activity(
        RunnerActivity.run,
        data,
        id=f"runner-test-{uuid4()}",
        task_queue=queue,
        start_to_close_timeout=timedelta(seconds=30),
        heartbeat_timeout=timedelta(seconds=10),
        retry_policy=RetryPolicy(maximum_attempts=1),
        **kwargs,
    )


async def execute(client, queue, data):
    handle = await start(client, queue, data)
    return await asyncio.wait_for(handle.result(), 35)


@pytest.fixture
def endpoint() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {
                    "path": self.path,
                    "body": body,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                }
            )
            response: dict[str, Any]
            chunks: list[dict[str, Any]]
            if self.path.endswith("/chat/completions"):
                response = {
                    "id": "chat_test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            elif self.path.endswith("/responses"):
                response = {
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 1,
                    "model": body["model"],
                    "status": "completed",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                    "output": [
                        {
                            "id": "msg_test",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "done", "annotations": []}],
                        }
                    ],
                }
            else:
                response = {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "done"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "modelVersion": "gemini-2.5-flash",
                }
            if self.path.endswith("/chat/completions"):
                assert body["stream"]
                chunks = [
                    {
                        "id": "chat_test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": text},
                                "finish_reason": reason,
                            }
                        ],
                    }
                    for text, reason in [("do", None), ("ne", None), ("", "stop")]
                ]
                chunks[-1]["usage"] = {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                }
            elif self.path.endswith("/responses"):
                assert body["stream"]
                item = response["output"][0]
                part = item["content"][0]
                chunks = [
                    {"type": "response.created", "response": {**response, "output": []}},
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "content": []},
                    },
                    {
                        "type": "response.content_part.added",
                        "item_id": "msg_test",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {**part, "text": ""},
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_test",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "done",
                    },
                    {
                        "type": "response.output_text.done",
                        "item_id": "msg_test",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "done",
                    },
                    {
                        "type": "response.content_part.done",
                        "item_id": "msg_test",
                        "output_index": 0,
                        "content_index": 0,
                        "part": part,
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                for index, chunk in enumerate(chunks):
                    chunk["sequence_number"] = index
            else:
                assert "streamGenerateContent" in self.path
                chunks = [response]
            payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode()
            if self.path.endswith("/chat/completions"):
                payload += b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def make_config(base_url: str, protocol: str = "OpenAIChatModel") -> ExecutionConfig:
    google = protocol == "GoogleModel"
    return ExecutionConfig(
        provider_class=(
            "pydantic_ai.providers.google:GoogleProvider"
            if google
            else "pydantic_ai.providers.openai:OpenAIProvider"
        ),
        model_class=f"pydantic_ai.models.{'google' if google else 'openai'}:{protocol}",
        model_name="gemini-2.5-flash" if google else "gpt-4o-mini",
        api_key="test-secret",
        base_url=base_url,
        model_settings={"temperature": 0.25},
        context_window=12345,
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("model_class", [OpenAIChatModel, OpenAIResponsesModel, GoogleModel])
@pytest.mark.parametrize("with_tools", [False, True], ids=["new-session", "tool-history"])
async def test_database_history_reproduces_model_input(
    endpoint, runner_database, model_class, with_tools
):
    """Compare complete HTTP bodies after real storage, codec and SDK normalization.

    Recording commits directly through the real database. The restored input is
    selected in sequence order, then
    the newly generated response is excluded from the replay input.
    """
    base_url, requests = endpoint
    config = make_config(base_url, model_class.__name__)

    async def drain(ctx, events):
        async for _ in events:
            pass

    def work(path: str, rows: int) -> dict[str, Any]:
        """Read a report and return its row count."""
        return {"path": path, "rows": rows}

    instructions = "Keep all historical tool arguments and results."

    history: list[ModelMessage] = []
    if with_tools:
        history = [
            ModelRequest(
                parts=[SystemPromptPart("You are a file assistant."), UserPromptPart("first")]
            ),
            ModelResponse(parts=[TextPart("I will read the report.")]),
            ModelRequest(parts=[UserPromptPart("read the report")]),
            ModelResponse(
                parts=[
                    ToolCallPart("work", {"path": "report.csv", "rows": 3}, "call_report"),
                    # SDK normalization synthesizes an interrupted return for this call.
                    ToolCallPart("work", {"path": "missing.csv", "rows": 0}, "call_missing"),
                ]
            ),
            ModelRequest(
                parts=[ToolReturnPart("work", {"path": "report.csv", "rows": 3}, "call_report")]
            ),
            ModelRequest(parts=[UserPromptPart("Keep the previous tool result.")]),
        ]

    provider_config = config.provider_config()
    provider_class, resolved_model_class = resolve_classes(provider_config)
    async with (
        build_provider(provider_class, provider_config) as provider,
        build_model(
            resolved_model_class,
            config.model_name,
            provider,
            profile={"context_window": config.context_window},
        ) as model,
    ):
        recorder = Recorder()
        recording_agent = Agent(
            model,
            deps_type=RunnerDeps,
            system_prompt="You are a file assistant.",
            instructions=instructions,
            tools=[work],
            capabilities=[
                MessageRecordCapability(recorder),
                Hooks(
                    before_model_request=recorder.predict_response_seq,
                    ordering=CapabilityOrdering(
                        position="innermost", requires=[MessageRecordCapability]
                    ),
                ),
            ],
        )
        result = await recording_agent.run(
            "continue",
            message_history=history,
            deps=runner_database.deps(),
            model_settings=cast(ModelSettings, config.model_settings),
            event_stream_handler=drain,
        )
        assert result.output == "done" and len(requests) == 1
        if with_tools:
            result = await recording_agent.run(
                "follow-up",
                message_history=result.all_messages(),
                deps=runner_database.deps(),
                model_settings=cast(ModelSettings, config.model_settings),
                event_stream_handler=drain,
            )
            assert result.output == "done" and len(requests) == 2
        original_request = requests[-1]
        async with runner_database.sessions.begin() as db:
            rows = (
                (
                    await db.execute(
                        select(AgentHistoryRow)
                        .where(
                            col(AgentHistoryRow.session_id) == runner_database.session_id,
                        )
                        .order_by(col(AgentHistoryRow.seq))
                    )
                )
                .scalars()
                .all()
            )
        assert rows
        assert [row.seq for row in rows] == list(range(len(rows)))
        assert rows[-1].kind == "response"
        rows = rows[:-1]
        restored = ModelMessagesTypeAdapter.validate_python(
            [
                {
                    **row.message_metadata,
                    **row.message,
                    "kind": row.kind,
                    **({"finish_reason": row.finish_reason} if row.kind == "response" else {}),
                }
                for row in rows
            ]
        )
        assert isinstance(restored[-1], ModelRequest)
        # Excluding the response leaves the recorded request to send again.
        reproduction_agent = Agent(
            model,
            system_prompt="You are a file assistant.",
            instructions=instructions,
            tools=[work],
        )
        replayed = await reproduction_agent.run(
            message_history=restored,
            model_settings=cast(ModelSettings, config.model_settings),
            event_stream_handler=drain,
        )
        assert replayed.output == "done"

    assert len(requests) == (3 if with_tools else 2)
    assert requests[-1]["path"] == original_request["path"]
    # Keep every input/configuration field, including provider-specific identifiers.
    assert requests[-1]["body"] == original_request["body"]
    sent = json.dumps(original_request["body"])
    assert "Keep all historical tool arguments and results." in sent
    assert "You are a file assistant." in sent
    assert "work" in sent
    if with_tools:
        assert "report.csv" in sent and "missing.csv" in sent
        assert "Keep the previous tool result." in sent


def test_input_round_trip_and_validation():
    data = RunnerInput(
        session_id=uuid4(),
        runner_state_version=0,
        runner_state=None,
        user_prompt="hello",
        config=make_config("http://localhost:9999"),
    )
    assert RunnerInput.model_validate_json(data.model_dump_json()) == data
    assert "test-secret" not in repr(data)
    assert data.config.model_dump()["api_key"] == "test-secret"
    for overrides in (
        {"api_key": " "},
        {"base_url": "http://user:pass@localhost"},
        {"context_window": 0},
        {"provider_kwargs": {"http_client": "invalid"}},
        {"model_class": "without_separator"},
    ):
        with pytest.raises(ValidationError):
            ExecutionConfig.model_validate(data.config.model_dump() | overrides)
    for version in (-1, True, 1.0, "1"):
        with pytest.raises(ValidationError):
            RunnerInput.model_validate(data.model_dump() | {"runner_state_version": version})
    for field in ("session_id", "runner_state_version", "runner_state"):
        with pytest.raises(ValidationError):
            RunnerInput.model_validate(data.model_dump(exclude={field}))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_activity_restores_state_and_records_after_run_tail_once(
    endpoint, runner_database, monkeypatch
):
    from kapy.agent_history.repository import HistoryRepository

    base_url, requests = endpoint
    broadcasts: list[OutputEvent] = []
    publish = runner_database.outputs._publish

    async def observe_publish(session_id, payload):
        await publish(session_id, payload)
        broadcasts.extend(TypeAdapter(list[OutputEvent]).validate_json(payload))

    async def append_tail(
        ctx: RunContext[RunnerDeps], *, result: AgentRunResult[str]
    ) -> AgentRunResult[str]:
        if ctx.prompt == "first":
            result.all_messages().append(
                ModelRequest([UserPromptPart("after-run tail")], metadata={"source": "after_run"})
            )
        return result

    tail_agent = Agent(
        name=agent.name,
        model=agent.model,
        deps_type=RunnerDeps,
        capabilities=[*agent.root_capability.capabilities, Hooks(after_run=append_tail)],
    )
    monkeypatch.setattr(import_module("kapy.runner.activities"), "agent", tail_agent)
    monkeypatch.setattr(runner_database.outputs, "_publish", observe_publish)
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-restore-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        activities=[runner.run],
    ):
        for prompt in ("first", "follow-up"):
            async with runner_database.sessions.begin() as db:
                state, version = await SessionRepository(db).read_runner_state(
                    runner_database.session_id
                )
            assert (
                await execute(
                    client,
                    queue,
                    RunnerInput(
                        session_id=runner_database.session_id,
                        runner_state_version=version,
                        runner_state=state,
                        user_prompt=prompt,
                        config=make_config(base_url),
                    ),
                )
                == "done"
            )
            if prompt == "first":
                async with runner_database.sessions.begin() as db:
                    first_state, first_version = await SessionRepository(db).read_runner_state(
                        runner_database.session_id
                    )
                    first_entries = await HistoryRepository(db).read_history_entries(
                        runner_database.session_id
                    )
                assert first_state is not None and first_version == 1
                first_messages = ModelMessagesTypeAdapter.validate_json(first_state)
                tail = first_messages[-1]
                assert isinstance(tail, ModelRequest) and len(tail.parts) == 1
                part = tail.parts[0]
                assert isinstance(part, UserPromptPart) and part.content == "after-run tail"
                assert first_messages[-1].metadata == {"source": "after_run"}
                assert [entry.seq for entry in first_entries] == [0, 1]
                assert [e.message.seq for e in broadcasts if isinstance(e, MessageCommitted)] == [
                    0,
                    1,
                ]
    assert len(requests) == 2
    assert [(m["role"], m["content"]) for m in requests[1]["body"]["messages"]] == [
        ("user", "first"),
        ("assistant", "done"),
        ("user", "after-run tail"),
        ("user", "follow-up"),
    ]
    async with runner_database.sessions.begin() as db:
        state, version = await SessionRepository(db).read_runner_state(runner_database.session_id)
        entries = await HistoryRepository(db).read_history_entries(runner_database.session_id)
    assert state is not None and version == 2
    messages = ModelMessagesTypeAdapter.validate_json(state)
    assert [m.metadata for m in messages] == [
        {"seq": 0},
        {"seq": 1},
        {"source": "after_run", "seq": 2},
        {"seq": 3},
        {"seq": 4},
    ]
    assert [entry.seq for entry in entries] == list(range(5))
    assert [e.message.seq for e in broadcasts if isinstance(e, MessageCommitted)] == list(range(5))
    # History normalizes usage into token columns; runner_state retains SDK usage.
    assert ModelMessagesTypeAdapter.dump_python(
        [entry.message for entry in entries], exclude={"__all__": {"usage"}}
    ) == ModelMessagesTypeAdapter.dump_python(messages, exclude={"__all__": {"usage"}})


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "layers"),
    [
        ("not-json", {}),
        ('{"history": []}', {}),
        ('[{"kind": "invalid"}]', {}),
        (
            None,
            {
                "shell": {
                    "config": {"cwd": "relative"},
                    "state": {"scope_id": str(uuid4()), "jobs": {}},
                }
            },
        ),
        (None, {"shell": {"config": {"cwd": "/tmp"}, "state": {"jobs": {}}}}),
        (
            None,
            {
                "shell": {
                    "config": {"cwd": "/tmp"},
                    "state": {"scope_id": str(uuid4()), "jobs": {"job": -1}},
                }
            },
        ),
        (None, {"unknown": {"config": {}, "state": {}}}),
    ],
)
async def test_invalid_state_fails_activity_before_model_call(
    endpoint, runner_database, state, layers
):

    from kapy.agent_history.repository import HistoryRepository

    base_url, requests = endpoint
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-invalid-state-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        activities=[runner.run],
    ):
        with pytest.raises(ActivityFailureError) as error:
            await execute(
                client,
                queue,
                RunnerInput(
                    session_id=runner_database.session_id,
                    runner_state_version=0,
                    runner_state=state,
                    layers=layers,
                    user_prompt="hello",
                    config=make_config(base_url),
                ),
            )
    assert isinstance(error.value.cause, ApplicationError)
    assert error.value.cause.type == "UserError"
    assert requests == []
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == (
            None,
            0,
        )
        assert await HistoryRepository(db).read_history_entries(runner_database.session_id) == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_service_runs_successive_prompts_and_telegram_delivery(
    endpoint, runner_database, tmp_path
):
    """Real Worker/SDK/state/live wiring; only the external Telegram send is mocked."""
    from unittest.mock import AsyncMock

    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kapy.application.resources import Resources
    from kapy.application.sessions import create_session_service
    from kapy.control.models import CreateModel, CreateProvider, ModelService
    from kapy.control.sessions import CreateSession
    from kapy.interfaces.telegram.client import TelegramClient
    from kapy.interfaces.telegram.controller import TelegramController
    from kapy.interfaces.telegram.delivery import TelegramDelivery
    from kapy.interfaces.telegram.repository import TelegramRepository, delivery_key
    from kapy.interfaces.telegram.schema import migrate
    from kapy.interfaces.telegram.settings import TelegramSettings
    from kapy.interfaces.telegram.storage import open_storage

    base_url, requests = endpoint
    models = ModelService(runner_database.sessions)
    provider = await models.create_provider(
        CreateProvider(
            name="durable-service-test",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIChatModel",
            api_key=SecretStr("local-test"),
            base_url=base_url,
        )
    )
    await models.create_model(
        CreateModel(
            provider_id=provider.id,
            model_name="gpt-4o-mini",
            settings={"temperature": 0.1, "max_tokens": 20},
            context_window=12345,
        )
    )
    temporal = await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
        data_converter=pydantic_data_converter,
    )
    queue = f"service-test-{uuid4()}"
    sessions = create_session_service(
        Resources(runner_database.sessions, runner_database.valkey, temporal),
        runner_database.settings.model_copy(update={"temporal_task_queue": queue}),
    )
    template = CreateSession(
        provider_id=provider.id,
        model_name="gpt-4o-mini",
        model_settings={"temperature": 0.5},
    )
    path = tmp_path / "telegram.sqlite3"
    await migrate(path, "upgrade")
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    async with (
        open_storage(path) as storage,
        Worker(
            temporal,
            task_queue=queue,
            activities=[runner.run],
        ),
    ):
        repository = TelegramRepository(async_sessionmaker(storage, expire_on_commit=False))
        telegram = AsyncMock(spec=TelegramClient)
        telegram.send.return_value = 123
        controller = TelegramController(
            client=telegram,
            sessions=sessions,
            models=models,
            repository=repository,
            settings=TelegramSettings(
                bot_token="unused",
                allowed_chat_ids={123},
                database_path=path,
                session_template=template,
            ),
            bot_id=42,
            username="test_bot",
        )
        await repository.ingest(
            42,
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": 1},
                        "text": "first prompt",
                    },
                }
            ],
        )
        assert await controller.process_once()
        session_id = await repository.route(42, 123, 0)
        assert session_id is not None
        first = temporal.get_activity_handle(f"kapy-runner:{session_id}")
        assert await asyncio.wait_for(first.result(), 30) == "done"
        (row,) = await repository.deliveries(42)
        follower = asyncio.create_task(
            TelegramDelivery(telegram, sessions, repository, 42).consume(row)
        )
        try:
            async with asyncio.timeout(5):
                while (  # noqa: ASYNC110
                    (await repository.get_delivery(delivery_key(row))).after_seq < 1
                ):
                    await asyncio.sleep(0.01)
            saved = await repository.get_delivery(delivery_key(row))
            assert saved.after_seq == 1
            # The first final reply is delivered without waiting for another prompt.
            second = await sessions.start_runner(session_id, user_prompt="second prompt")
            assert await asyncio.wait_for(second.result(), 30) == "done"
            assert len(requests) == 2
            assert requests[0]["headers"]["authorization"] == "Bearer local-test"
            assert requests[0]["body"]["temperature"] == 0.5
            assert requests[0]["body"]["max_completion_tokens"] == 20
            assert [message["content"] for message in requests[1]["body"]["messages"]] == [
                "first prompt",
                "done",
                "second prompt",
            ]
            async with runner_database.sessions.begin() as db:
                state, version = await SessionRepository(db).read_runner_state(session_id)
            assert state is not None and version == 2
            assert not await sessions.is_runner_running(session_id)
            async with asyncio.timeout(5):
                while (  # noqa: ASYNC110
                    (await repository.get_delivery(delivery_key(row))).after_seq < 3
                ):
                    await asyncio.sleep(0.01)
            assert [
                call.args[2]
                for call in telegram.send.call_args_list
                if "draft_id" not in call.kwargs
            ] == ["done", "done"]
            assert telegram.send.call_args.args[2] == "done"
            assert "draft_id" not in telegram.send.call_args.kwargs
            saved = await repository.get_delivery(delivery_key(row))
            assert saved.after_seq == 3
            async with runner_database.sessions.begin() as db:
                from kapy.agent_history.repository import HistoryRepository

                entries = await HistoryRepository(db).read_history_entries(session_id)
            assert [entry.seq for entry in entries] == [0, 1, 2, 3]
        finally:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_converter_round_trip_includes_binary_message_snapshot():
    messages: list[ModelMessage] = [
        ModelRequest(
            [UserPromptPart(["image", BinaryContent(data=b"\xff\x00\xfe", media_type="image/png")])]
        )
    ]
    data = RunnerInput(
        session_id=uuid4(),
        runner_state_version=0,
        runner_state=ModelMessagesTypeAdapter.dump_json(messages).decode(),
        user_prompt="hello",
        config=make_config("http://localhost:9999"),
    )
    (restored,) = await pydantic_data_converter.decode(
        await pydantic_data_converter.encode([data]), [RunnerInput]
    )
    assert restored == data


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("model_class", [OpenAIChatModel, OpenAIResponsesModel, GoogleModel])
async def test_activity_protocol_resources_and_history(
    endpoint, monkeypatch, model_class, runner_database
):
    base_url, requests = endpoint
    original_request = model_class.request_stream
    models = []

    @asynccontextmanager
    async def checked_request(self, *args, **kwargs):
        assert activity.in_activity()
        assert activity.info().workflow_id is None
        assert self.context_window == 12345
        models.append(self)
        async with original_request(self, *args, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(model_class, "request_stream", checked_request)
    broadcasts: list[OutputEvent] = []
    publish = runner_database.outputs._publish

    async def observe_publish(session_id, payload):
        await publish(session_id, payload)
        broadcasts.extend(TypeAdapter(list[OutputEvent]).validate_json(payload))

    monkeypatch.setattr(runner_database.outputs, "_publish", observe_publish)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-protocol-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    async with Worker(client, task_queue=queue, activities=[runner.run]):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=0,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url, model_class.__name__),
            ),
        )
        assert await asyncio.wait_for(handle.result(), 35) == "done"
        description = await handle.describe()
        assert description.status == ActivityExecutionStatus.COMPLETED
        assert description.attempt == 1
        assert description.retry_policy.maximum_attempts == 1
        assert description.activity_type == "kapy.run_agent"
        assert description.heartbeat_timeout == timedelta(seconds=10)
    async with runner_database.sessions.begin() as db:
        state, version = await SessionRepository(db).read_runner_state(runner_database.session_id)
        stored = await HistoryRepository(db).read_history_entries(runner_database.session_id)
    assert state is not None and version == 1
    assert [entry.seq for entry in stored] == [0, 1]
    assert [entry.message.metadata for entry in stored] == [{"seq": 0}, {"seq": 1}]
    assert [event.message.seq for event in broadcasts if isinstance(event, MessageCommitted)] == [
        0,
        1,
    ]
    assert [
        (event.response_seq, event.text) for event in broadcasts if isinstance(event, TextDelta)
    ] == [(1, "done")]
    assert len(requests) == len(models) == 1
    assert models[0].provider._own_http_client.is_closed
    body = requests[0]["body"]
    if model_class is GoogleModel:
        assert body["generationConfig"]["temperature"] == 0.25
        assert body["contents"][0]["parts"] == [{"text": "hello"}]
    else:
        assert body["temperature"] == 0.25
        assert body["model"] == "gpt-4o-mini"
        assert requests[0]["headers"]["authorization"] == "Bearer test-secret"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, UserError])
async def test_model_failure_executes_once_closes_resources_and_keeps_previous_snapshot(
    endpoint, monkeypatch, runner_database, failure
):
    base_url, requests = endpoint
    models = []
    async with runner_database.sessions.begin() as db:
        await SessionRepository(db).save_runner_state(
            runner_database.session_id, expected_version=0, runner_state="previous"
        )

    @asynccontextmanager
    async def fail_request(self, *args, **kwargs):
        assert activity.in_activity()
        models.append(self)
        raise failure("model failed")
        yield

    monkeypatch.setattr(OpenAIChatModel, "request_stream", fail_request)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-failure-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    async with Worker(client, task_queue=queue, activities=[runner.run]):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=1,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
        )
        with pytest.raises(ActivityFailureError):
            await asyncio.wait_for(handle.result(), 35)
        assert (await handle.describe()).status == ActivityExecutionStatus.FAILED
    assert len(models) == 1 and models[0].provider._own_http_client.is_closed
    assert requests == []
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == (
            "previous",
            1,
        )
        assert await HistoryRepository(db).read_history_entries(runner_database.session_id) == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancellation_waits_for_model_cleanup_and_preserves_snapshot(
    endpoint, monkeypatch, runner_database
):
    base_url, requests = endpoint
    started, closed = asyncio.Event(), asyncio.Event()
    models = []

    @asynccontextmanager
    async def blocked_request(self, *args, **kwargs):
        models.append(self)
        started.set()
        try:
            await asyncio.Future()
            yield
        finally:
            closed.set()

    monkeypatch.setattr(OpenAIChatModel, "request_stream", blocked_request)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-cancel-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    async with Worker(client, task_queue=queue, activities=[runner.run]):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=0,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
        )
        try:
            await asyncio.wait_for(started.wait(), 10)
            description = await handle.describe()
            assert description.last_heartbeat_time is not None
            await handle.cancel()
            with pytest.raises(ActivityFailureError) as error:
                await asyncio.wait_for(handle.result(), 20)
            assert isinstance(error.value.cause, CancelledError)
            assert closed.is_set() and models[0].provider._own_http_client.is_closed
        finally:
            if (await handle.describe()).status == ActivityExecutionStatus.RUNNING:
                await handle.cancel()
    assert requests == []
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == (
            None,
            0,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_save_failure_does_not_retry_agent_or_undo_history(
    endpoint, monkeypatch, runner_database
):
    base_url, requests = endpoint
    saves = []

    async def fail_save(self, session_id, **kwargs):
        saves.append(kwargs)
        raise RuntimeError("database save unavailable")

    monkeypatch.setattr(SessionRepository, "save_runner_state", fail_save)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-save-failure-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    async with Worker(client, task_queue=queue, activities=[runner.run]):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=0,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
        )
        with pytest.raises(ActivityFailureError):
            await asyncio.wait_for(handle.result(), 35)
    assert len(requests) == len(saves) == 1
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == (
            None,
            0,
        )
        assert [
            entry.seq
            for entry in await HistoryRepository(db).read_history_entries(
                runner_database.session_id
            )
        ] == [0, 1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_client_and_worker_entry(endpoint, runner_database):
    base_url, requests = endpoint
    settings = runner_database.settings.model_copy(
        update={"temporal_task_queue": f"entry-test-{uuid4()}"}
    )
    client = await connect_temporal(settings)
    worker_task = asyncio.create_task(serve(settings))
    try:
        result = await execute(
            client,
            settings.temporal_task_queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=0,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
        )
        assert result == "done" and len(requests) == 1
        async with runner_database.sessions.begin() as db:
            state, version = await SessionRepository(db).read_runner_state(
                runner_database.session_id
            )
        assert state is not None and version == 1
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker_task, 20)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [True, False], ids=["failed-save", "completed"])
async def test_deleted_activity_cannot_restart_session_with_committed_progress(
    endpoint, monkeypatch, runner_database, save_fails
):
    from pydantic import SecretStr
    from temporalio.api.workflowservice.v1 import DeleteActivityExecutionRequest
    from temporalio.service import RPCError, RPCStatusCode

    from kapy.control.models import CreateModel, CreateProvider, ModelService
    from kapy.control.sessions import CreateSession, RunnerConflict, SessionService

    base_url, requests = endpoint
    models = ModelService(runner_database.sessions)
    provider = await models.create_provider(
        CreateProvider(
            name="deleted-activity",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIChatModel",
            api_key=SecretStr("local-test"),
            base_url=base_url,
        )
    )
    await models.create_model(
        CreateModel(provider_id=provider.id, model_name="gpt-4o-mini", context_window=12345)
    )
    client = await connect_temporal(runner_database.settings)
    queue = f"deleted-activity-{uuid4()}"
    sessions = SessionService(
        runner_database.sessions, temporal_client=client, temporal_task_queue=queue
    )
    session = await sessions.create_session(
        CreateSession(provider_id=provider.id, model_name="gpt-4o-mini")
    )
    if save_fails:

        async def fail_save(self, session_id, **kwargs):
            raise RuntimeError("final save unavailable")

        monkeypatch.setattr(SessionRepository, "save_runner_state", fail_save)
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs)
    async with Worker(client, task_queue=queue, activities=[runner.run]):
        handle = await sessions.start_runner(session.id, user_prompt="prompt A")
        if save_fails:
            with pytest.raises(ActivityFailureError):
                await asyncio.wait_for(handle.result(), 35)
        else:
            assert await asyncio.wait_for(handle.result(), 35) == "done"
        async with runner_database.sessions.begin() as db:
            snapshot = await SessionRepository(db).read_runner_state(session.id)
            history = await HistoryRepository(db).read_history_entries(session.id)
        assert [entry.seq for entry in history] == [0, 1]
        if save_fails:
            assert snapshot == (None, 0)
        else:
            assert snapshot[0] is not None and snapshot[1] == 1
        assert handle.run_id is not None
        await client.workflow_service.delete_activity_execution(
            DeleteActivityExecutionRequest(
                namespace=client.namespace, activity_id=handle.id, run_id=handle.run_id
            )
        )
        with pytest.raises(RPCError) as error:
            await client.get_activity_handle(handle.id).describe()
        assert error.value.status == RPCStatusCode.NOT_FOUND
        with pytest.raises(RunnerConflict, match="record is missing"):
            await sessions.start_runner(session.id, user_prompt="prompt B")
    assert len(requests) == 1
    assert requests[0]["body"]["messages"][-1]["content"] == "prompt A"
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(session.id) == snapshot
        assert await HistoryRepository(db).read_history_entries(session.id) == history
