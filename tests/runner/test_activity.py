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
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
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
    RunnerActivity,
    RunnerDeps,
    RunnerInput,
    RunnerPersistenceCapability,
    agent,
)
from kapy.runner.recording import Recorder
from kapy.runner.worker import serve


async def start(client, queue, data, *, database, **kwargs):
    """Prepare business data; the native request contains only session and prompt."""
    from kapy.control.models.models import ModelRow, ProviderRow
    from kapy.control.sessions.models import SessionRow
    from kapy.control.sessions.types import RunAgentActivity
    from kapy.lifecycle import LifecycleStatus

    key = uuid4()
    async with database.sessions.begin() as db:
        session = await db.get(SessionRow, data.session_id)
        session.status = LifecycleStatus.READY
        session.current_activity_id = session.current_activity_run_id = None
        session.model_name = data.config.model_name
        session.layers = {name: entry["config"] for name, entry in data.layers.items()}
        session.layer_states = {name: entry["state"] for name, entry in data.layers.items()}
        if data.runner_state is not None:
            session.runner_state = data.runner_state
        await db.merge(
            ProviderRow(
                id=session.provider_id,
                name="local-test",
                provider_class=data.config.provider_class,
                model_class=data.config.model_class,
                api_key=data.config.api_key,
                base_url=data.config.base_url,
                provider_kwargs=data.config.provider_kwargs,
            )
        )
        await db.merge(
            ModelRow(
                provider_id=session.provider_id,
                model_name=data.config.model_name,
                name="test",
                settings=data.config.model_settings,
                context_window=data.config.context_window,
            )
        )
    return await client.start_activity(
        RunnerActivity.run,
        RunAgentActivity(session_id=data.session_id, user_prompt=data.user_prompt),
        id=str(key),
        task_queue=queue,
        start_to_close_timeout=timedelta(seconds=30),
        heartbeat_timeout=timedelta(seconds=10),
        retry_policy=RetryPolicy(maximum_attempts=1),
        **kwargs,
    )


async def execute(client, queue, data, *, database):
    handle = await start(client, queue, data, database=database)
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
                RunnerPersistenceCapability(recorder),
                Hooks(
                    before_model_request=recorder.predict_response_seq,
                    ordering=CapabilityOrdering(
                        position="innermost", requires=[RunnerPersistenceCapability]
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
    for field in ("session_id", "runner_state"):
        with pytest.raises(ValidationError):
            RunnerInput.model_validate(data.model_dump(exclude={field}))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("saved_conversation", [None, "previous-sdk-conversation"])
async def test_activity_uses_session_conversation_for_first_later_and_restored_runs(
    endpoint, runner_database, monkeypatch, saved_conversation
):
    base_url, requests = endpoint
    seen = []

    async def observe(ctx, request):
        seen.append((ctx.conversation_id, ctx.run_id))
        return request

    observed_agent = Agent(
        name=agent.name,
        deps_type=RunnerDeps,
        capabilities=[*agent.root_capability.capabilities, Hooks(before_model_request=observe)],
    )
    monkeypatch.setattr(import_module("kapy.runner.activities"), "agent", observed_agent)
    initial = (
        [
            ModelRequest([UserPromptPart("old prompt")], conversation_id=saved_conversation),
            ModelResponse([TextPart("old answer")], conversation_id=saved_conversation),
        ]
        if saved_conversation is not None
        else []
    )
    snapshot = ModelMessagesTypeAdapter.dump_json(initial).decode() if initial else None
    client = await connect_temporal(runner_database.settings)
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    queue = f"runner-conversation-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        activities=[runner.create, runner.run, runner.close],
    ):
        for prompt in ("first", "follow-up"):
            assert (
                await execute(
                    client,
                    queue,
                    RunnerInput(
                        session_id=runner_database.session_id,
                        runner_state=snapshot,
                        user_prompt=prompt,
                        config=make_config(base_url),
                    ),
                    database=runner_database,
                )
                == "done"
            )
            async with runner_database.sessions.begin() as db:
                snapshot = await SessionRepository(db).read_runner_state(runner_database.session_id)
            assert snapshot is not None
            messages = ModelMessagesTypeAdapter.validate_json(snapshot)
            assert [message.conversation_id for message in messages] == (
                [saved_conversation] * len(initial)
                + [str(runner_database.session_id)] * (len(messages) - len(initial))
            )
    assert len(requests) == len(seen) == 2
    assert [conversation for conversation, _ in seen] == [str(runner_database.session_id)] * 2
    assert all(run_id and run_id != str(runner_database.session_id) for _, run_id in seen)
    assert seen[0][1] != seen[1][1]


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
    client = await connect_temporal(runner_database.settings)
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    queue = f"runner-restore-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        activities=[runner.create, runner.run, runner.close],
    ):
        for prompt in ("first", "follow-up"):
            async with runner_database.sessions.begin() as db:
                state = await SessionRepository(db).read_runner_state(runner_database.session_id)
            assert (
                await execute(
                    client,
                    queue,
                    RunnerInput(
                        session_id=runner_database.session_id,
                        runner_state=state,
                        user_prompt=prompt,
                        config=make_config(base_url),
                    ),
                    database=runner_database,
                )
                == "done"
            )
            if prompt == "first":
                async with runner_database.sessions.begin() as db:
                    first_state = await SessionRepository(db).read_runner_state(
                        runner_database.session_id
                    )
                    first_entries = await HistoryRepository(db).read_history_entries(
                        runner_database.session_id
                    )
                assert first_state is not None
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
        state = await SessionRepository(db).read_runner_state(runner_database.session_id)
        entries = await HistoryRepository(db).read_history_entries(runner_database.session_id)
    assert state is not None
    messages = ModelMessagesTypeAdapter.validate_json(state)
    assert all(message.state == "complete" for message in messages)
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
                    "state": {"jobs": {}},
                }
            },
        ),
        (
            None,
            {"shell": {"config": {"cwd": "/tmp"}, "state": {"scope_id": str(uuid4()), "jobs": {}}}},
        ),
        (
            None,
            {
                "shell": {
                    "config": {"cwd": "/tmp"},
                    "state": {"jobs": {"job": -1}},
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
    client = await connect_temporal(runner_database.settings)
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    queue = f"runner-invalid-state-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        activities=[runner.create, runner.run, runner.close],
    ):
        with pytest.raises(ActivityFailureError) as error:
            await execute(
                client,
                queue,
                RunnerInput(
                    session_id=runner_database.session_id,
                    runner_state=state,
                    layers=layers,
                    user_prompt="hello",
                    config=make_config(base_url),
                ),
                database=runner_database,
            )
    assert isinstance(error.value.cause, ApplicationError)
    assert error.value.cause.type == "InvalidParameters"
    assert requests == []
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == state
        assert (
            await SessionRepository(db).get_session(runner_database.session_id)
        ).current_activity_id is None
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
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, temporal)
    async with (
        open_storage(path) as storage,
        Worker(
            temporal,
            task_queue=queue,
            activities=[runner.create, runner.run, runner.close],
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
        async with repository.sessions.begin() as db:
            from kapy.interfaces.telegram.models import InboxRow

            inbox = await db.get(InboxRow, (42, 1))
            assert inbox is not None and inbox.resolved_action is not None
            first_key = inbox.resolved_action["run_activity_id"]
        first = temporal.get_activity_handle(first_key)
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
            second = await sessions.start_runner(
                session_id, user_prompt="second prompt", activity_id=uuid4()
            )
            second_handle = temporal.get_activity_handle(second.id, run_id=second.run_id)
            assert await asyncio.wait_for(second_handle.result(), 30) == "done"
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
                state = await SessionRepository(db).read_runner_state(session_id)
            assert state is not None
            assert (await sessions.get_session(session_id)).current_activity_id is None
            async with asyncio.timeout(5):
                while (  # noqa: ASYNC110
                    (await repository.get_delivery(delivery_key(row))).after_seq < 3
                ):
                    await asyncio.sleep(0.01)
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
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    async with Worker(
        client, task_queue=queue, activities=[runner.create, runner.run, runner.close]
    ):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url, model_class.__name__),
            ),
            database=runner_database,
        )
        assert await asyncio.wait_for(handle.result(), 35) == "done"
        description = await handle.describe()
        assert description.status == ActivityExecutionStatus.COMPLETED
        assert description.attempt == 1
        assert description.retry_policy.maximum_attempts == 1
        assert description.activity_type == "kapy.run_agent"
        assert description.heartbeat_timeout == timedelta(seconds=10)
    async with runner_database.sessions.begin() as db:
        state = await SessionRepository(db).read_runner_state(runner_database.session_id)
        stored = await HistoryRepository(db).read_history_entries(runner_database.session_id)
    assert state is not None
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
        from kapy.control.sessions.models import SessionRow

        (await db.get(SessionRow, runner_database.session_id)).runner_state = "[]"

    @asynccontextmanager
    async def fail_request(self, *args, **kwargs):
        assert activity.in_activity()
        models.append(self)
        raise failure("model failed")
        yield

    monkeypatch.setattr(OpenAIChatModel, "request_stream", fail_request)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-failure-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    async with Worker(
        client, task_queue=queue, activities=[runner.create, runner.run, runner.close]
    ):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError):
            await asyncio.wait_for(handle.result(), 35)
        assert (await handle.describe()).status == ActivityExecutionStatus.FAILED
    assert len(models) == 1 and models[0].provider._own_http_client.is_closed
    assert requests == []
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == "[]"
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
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    async with Worker(
        client, task_queue=queue, activities=[runner.create, runner.run, runner.close]
    ):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
            database=runner_database,
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
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_save_failure_does_not_retry_agent_or_undo_history(
    endpoint, monkeypatch, runner_database
):
    base_url, requests = endpoint
    saves = []

    async def fail_save(self, *args, **kwargs):
        saves.append(kwargs)
        raise RuntimeError("database save unavailable")

    monkeypatch.setattr(SessionRepository, "complete", fail_save)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-save-failure-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    async with Worker(
        client, task_queue=queue, activities=[runner.create, runner.run, runner.close]
    ):
        handle = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError):
            await asyncio.wait_for(handle.result(), 35)
    assert len(requests) == 1 and len(saves) == 2
    async with runner_database.sessions.begin() as db:
        state = await SessionRepository(db).read_runner_state(runner_database.session_id)
        assert state is not None
        snapshot = ModelMessagesTypeAdapter.validate_json(state)
        assert snapshot[-1].state == "interrupted"
        assert isinstance(snapshot[-1], ModelRequest)
        assert isinstance(snapshot[-1].parts[0], RetryPromptPart)
        assert snapshot[-1].parts[0].content == (
            "Unexpected interruption. Your messages may or may not be delivered. "
            "If your messages were intended to make changes, "
            "retry only if the operation is idempotent; otherwise, "
            "verify the outcome before taking further action."
        )
        assert snapshot[-2].parts == [TextPart("done")]
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
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        assert result == "done" and len(requests) == 1
        async with runner_database.sessions.begin() as db:
            state = await SessionRepository(db).read_runner_state(runner_database.session_id)
        assert state is not None
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker_task, 20)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queued_run_loads_latest_business_snapshot_and_keeps_credentials_local(
    endpoint,
    runner_database,
    monkeypatch,
):
    from kapy.control.models.models import ModelRow, ProviderRow
    from kapy.control.sessions.models import SessionRow

    base_url, requests = endpoint
    client = await connect_temporal(runner_database.settings)
    queue = f"latest-input-{uuid4()}"
    handle = await start(
        client,
        queue,
        RunnerInput(
            session_id=runner_database.session_id,
            runner_state=None,
            user_prompt="queued",
            config=make_config(base_url),
        ),
        database=runner_database,
    )
    description = await handle.describe(include_input=True)
    assert await description.input() == [
        {"session_id": str(runner_database.session_id), "user_prompt": "queued"}
    ]
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, runner_database.session_id)
        (await db.get(ProviderRow, row.provider_id)).api_key = "latest-key"
        (await db.get(ModelRow, (row.provider_id, row.model_name))).settings = {"temperature": 0.75}
        row.runner_state = ModelMessagesTypeAdapter.dump_json(
            [
                ModelRequest([UserPromptPart("latest snapshot")]),
            ]
        ).decode()
    entered, release = asyncio.Event(), asyncio.Event()
    original = OpenAIChatModel.request_stream

    @asynccontextmanager
    async def blocked(self, *args, **kwargs):
        entered.set()
        await release.wait()
        async with original(self, *args, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(OpenAIChatModel, "request_stream", blocked)
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    async with Worker(client, task_queue=queue, activities=[runner.run]):
        try:
            await asyncio.wait_for(entered.wait(), 10)
            async with runner_database.sessions.begin() as db:
                row = await db.get(SessionRow, runner_database.session_id)
                (await db.get(ProviderRow, row.provider_id)).api_key = "after-admission-key"
            release.set()
            assert await asyncio.wait_for(handle.result(), 30) == "done"
        finally:
            release.set()
    assert requests[0]["headers"]["authorization"] == "Bearer latest-key"
    assert requests[0]["body"]["temperature"] == 0.75
    assert [message["content"] for message in requests[0]["body"]["messages"]] == [
        "latest snapshot",
        "queued",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deleted_native_result_is_unconfirmed_and_uuid_can_be_reused(
    endpoint, runner_database
):
    from uuid import UUID

    from temporalio.api.workflowservice.v1 import DeleteActivityExecutionRequest

    from kapy.control.sessions import SessionService

    base_url, requests = endpoint
    client = await connect_temporal(runner_database.settings)
    queue = f"retention-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    service = SessionService(
        runner_database.sessions, temporal_client=client, temporal_task_queue=queue
    )
    async with Worker(client, task_queue=queue, activities=[runner.run]):
        first = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="first",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        assert await first.result() == "done"
        key = UUID(first.id)
        retained = await service.start_runner(
            runner_database.session_id, activity_id=key, user_prompt="ignored"
        )
        assert retained.run_id == first.run_id and await retained.result() == "done"
        await client.workflow_service.delete_activity_execution(
            DeleteActivityExecutionRequest(
                namespace=client.namespace,
                activity_id=first.id,
                run_id=first.run_id,
            )
        )
        with pytest.raises(LookupError):
            await service.get_activity(runner_database.session_id, key, run_id=first.run_id)
        second = await service.start_runner(
            runner_database.session_id, activity_id=key, user_prompt="new"
        )
        assert second.run_id != first.run_id and await second.result() == "done"
        assert len(requests) == 2
