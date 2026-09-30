"""Exercise the real Temporal sandbox and SDK protocols against a local HTTP server.

Start Compose's temporal service and set KAPY_TEMPORAL_ADDRESS=temporal:7233 when
running inside the runtime container. Every test owns a unique task queue and
Workflow ID; no remote model credentials or API calls are used.
"""

import asyncio
import json
import os
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, UserPromptPart
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from kapy.agent_runner import MessageCommitted, OutputEvent, TextDelta
from kapy.application.resources import connect_temporal
from kapy.application.settings import CommonSettings
from kapy.control.sessions.repository import SessionRepository
from kapy.runner_duarable import DurableExecutionConfig, RunnerInput, RunnerWorkflow, agent
from kapy.runner_duarable.activities import RunnerActivities
from kapy.runner_duarable.types import MessageBatch, SaveRunnerStateInput
from kapy.runner_duarable.worker import serve


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


def make_config(base_url: str, protocol: str = "OpenAIChatModel") -> DurableExecutionConfig:
    google = protocol == "GoogleModel"
    return DurableExecutionConfig(
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
            DurableExecutionConfig.model_validate(data.config.model_dump() | overrides)
    for version in (-1, True, 1.0, "1"):
        with pytest.raises(ValidationError):
            RunnerInput.model_validate(data.model_dump() | {"runner_state_version": version})
        with pytest.raises(ValidationError):
            SaveRunnerStateInput.model_validate(
                {"session_id": data.session_id, "expected_version": version, "runner_state": ""}
            )
    for field in ("session_id", "runner_state_version", "runner_state"):
        with pytest.raises(ValidationError):
            RunnerInput.model_validate(data.model_dump(exclude={field}))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("model_class", [OpenAIChatModel, OpenAIResponsesModel, GoogleModel])
async def test_workflow_protocol_and_replay(endpoint, monkeypatch, model_class, runner_database):
    base_url, requests = endpoint
    config = make_config(base_url, model_class.__name__)
    original_request = model_class.request_stream
    models = []

    @asynccontextmanager
    async def checked_request(self, *args, **kwargs):
        assert activity.in_activity(), "Model I/O escaped the Activity boundary"
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
    client = await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
        plugins=[PydanticAIPlugin()],
    )
    queue = f"runner-test-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[
            RunnerActivities(runner_database.sessions, runner_database.outputs).record_messages,
            RunnerActivities(runner_database.sessions, runner_database.outputs).save_runner_state,
        ],
        plugins=[AgentPlugin(agent)],
    ):
        handle = await client.start_workflow(
            RunnerWorkflow.run,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=0,
                runner_state=None,
                user_prompt="hello",
                config=config,
            ),
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(seconds=30),
        )
        assert await asyncio.wait_for(handle.result(), 35) == "done"
        history = await handle.fetch_history()
    async with runner_database.sessions.begin() as db:
        repo = SessionRepository(db)
        state, version = await repo.read_runner_state(runner_database.session_id)
        saved_at = (await repo.get_session(runner_database.session_id)).updated_at
    assert state is not None and version == 1
    from kapy.agent_runner.repository import AgentRepository

    async with runner_database.sessions.begin() as db:
        stored = await AgentRepository(db).read_history_entries(runner_database.session_id)
    assert [entry.seq for entry in stored] == [0, 1]
    assert [
        (e.message.seq, e.message.authoritative)
        for e in broadcasts
        if isinstance(e, MessageCommitted)
    ] == [(0, False), (0, True), (1, True)]
    assert [(e.response_seq, e.text) for e in broadcasts if isinstance(e, TextDelta)] == [
        (1, "done")
    ]
    published_before_replay = list(broadcasts)
    assert [entry.message.metadata for entry in stored] == [
        {"seq": 0, "authoritative": True},
        {"seq": 1, "authoritative": True},
    ]
    messages = ModelMessagesTypeAdapter.validate_json(state)
    assert any(
        isinstance(part, UserPromptPart) and part.content == "hello"
        for message in messages
        for part in message.parts
    )
    assert any(isinstance(part, TextPart) and part.content == "done" for part in messages[-1].parts)
    assert len(requests) == 1
    assert models and all(model.provider._own_http_client.is_closed for model in models)
    body = requests[0]["body"]
    if model_class is GoogleModel:
        assert body["generationConfig"]["temperature"] == 0.25
        assert body["contents"][0]["parts"] == [{"text": "hello"}]
    else:
        assert body["temperature"] == 0.25
        assert body["model"] == "gpt-4o-mini"
        assert requests[0]["headers"]["authorization"] == "Bearer test-secret"
    await Replayer(workflows=[RunnerWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
        history
    )
    assert len(requests) == 1 and broadcasts == published_before_replay
    async with runner_database.sessions.begin() as db:
        repo = SessionRepository(db)
        assert await repo.read_runner_state(runner_database.session_id) == (state, 1)
        assert (await repo.get_session(runner_database.session_id)).updated_at == saved_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_activity_closes_model(endpoint, monkeypatch, runner_database):
    from pydantic_ai.exceptions import UserError
    from temporalio.client import WorkflowFailureError

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
        raise UserError("invalid model request")
        yield  # pragma: no cover - retain the SDK async context manager contract

    monkeypatch.setattr(OpenAIChatModel, "request_stream", fail_request)
    client = await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
        plugins=[PydanticAIPlugin()],
    )
    queue = f"runner-failure-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[
            RunnerActivities(runner_database.sessions, runner_database.outputs).record_messages,
            RunnerActivities(runner_database.sessions, runner_database.outputs).save_runner_state,
        ],
        plugins=[AgentPlugin(agent)],
    ):
        with pytest.raises(WorkflowFailureError):
            await client.execute_workflow(
                RunnerWorkflow.run,
                RunnerInput(
                    session_id=runner_database.session_id,
                    runner_state_version=1,
                    runner_state=None,
                    user_prompt="hello",
                    config=make_config(base_url),
                ),
                id=queue,
                task_queue=queue,
                execution_timeout=timedelta(seconds=15),
            )
    assert len(models) == 1  # SDK classifies UserError as non-retryable.
    assert models[0].provider._own_http_client.is_closed
    assert requests == []
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == (
            "previous",
            1,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_client_and_worker_entry(endpoint, runner_database):
    base_url, requests = endpoint
    settings = CommonSettings.model_validate(
        {
            **os.environ,
            "KAPY_DATABASE_SCHEMA": runner_database.settings.database_schema,
            "KAPY_TEMPORAL_TASK_QUEUE": f"entry-test-{uuid4()}",
        }
    )
    client = await connect_temporal(settings)
    worker_task = asyncio.create_task(serve(settings))
    try:
        result = await asyncio.wait_for(
            client.execute_workflow(
                RunnerWorkflow.run,
                RunnerInput(
                    session_id=runner_database.session_id,
                    runner_state_version=0,
                    runner_state=None,
                    user_prompt="hello",
                    config=make_config(base_url),
                ),
                id=settings.temporal_task_queue,
                task_queue=settings.temporal_task_queue,
                execution_timeout=timedelta(seconds=15),
            ),
            timeout=20,
        )
        assert result == "done"
        assert len(requests) == 1
        async with runner_database.sessions.begin() as db:
            state, version = await SessionRepository(db).read_runner_state(
                runner_database.session_id
            )
            assert state is not None and version == 1
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker_task, timeout=20)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_retries_after_commit_without_incrementing_again(endpoint, runner_database):
    from temporalio.exceptions import ApplicationError

    base_url, requests = endpoint
    saver = RunnerActivities(runner_database.sessions, runner_database.outputs)
    observations = []
    inputs = []

    @activity.defn(name="kapy.save_runner_state")
    async def lose_first_ack(data: SaveRunnerStateInput) -> None:
        inputs.append(data)
        await saver.save_runner_state(data)
        async with runner_database.sessions.begin() as db:
            repo = SessionRepository(db)
            observations.append(
                (
                    await repo.read_runner_state(data.session_id),
                    (await repo.get_session(data.session_id)).updated_at,
                )
            )
        if activity.info().attempt == 1:
            raise ApplicationError("simulated lost acknowledgment after commit")

    client = await connect_temporal(runner_database.settings)
    queue = f"runner-retry-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[lose_first_ack, saver.record_messages],
        plugins=[AgentPlugin(agent)],
    ):
        result = await client.execute_workflow(
            RunnerWorkflow.run,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=0,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == "done" and len(requests) == 1
    assert len(inputs) == 2 and inputs[0] == inputs[1]
    assert len(observations) == 2 and observations[0] == observations[1]
    assert observations[0][0][1] == 1


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_save_business_errors_fail_workflow(endpoint, runner_database, missing):
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ActivityError, ApplicationError

    base_url, requests = endpoint
    async with runner_database.sessions.begin() as db:
        await SessionRepository(db).save_runner_state(
            runner_database.session_id, expected_version=0, runner_state="previous"
        )
    saver = RunnerActivities(runner_database.sessions, runner_database.outputs)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-save-failure-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[saver.record_messages, saver.save_runner_state],
        plugins=[AgentPlugin(agent)],
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await client.execute_workflow(
                RunnerWorkflow.run,
                RunnerInput(
                    session_id=uuid4() if missing else runner_database.session_id,
                    runner_state_version=0,
                    runner_state=None,
                    user_prompt="hello",
                    config=make_config(base_url),
                ),
                id=queue,
                task_queue=queue,
                execution_timeout=timedelta(seconds=15),
            )
    assert isinstance(exc_info.value.cause, ActivityError)
    cause = exc_info.value.cause.cause
    assert isinstance(cause, ApplicationError) and cause.non_retryable
    assert cause.type == ("SessionNotFound" if missing else "RunnerStateConflict")
    assert len(requests) == (0 if missing else 1)
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == (
            "previous",
            1,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_restores_state_and_continues_marks(endpoint, runner_database):
    from kapy.agent_runner.repository import AgentRepository

    base_url, requests = endpoint
    saver = RunnerActivities(runner_database.sessions, runner_database.outputs)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-restore-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[saver.record_messages, saver.save_runner_state],
        plugins=[AgentPlugin(agent)],
    ):
        for prompt in ("first", "follow-up"):
            async with runner_database.sessions.begin() as db:
                state, version = await SessionRepository(db).read_runner_state(
                    runner_database.session_id
                )
            assert (
                await client.execute_workflow(
                    RunnerWorkflow.run,
                    RunnerInput(
                        session_id=runner_database.session_id,
                        runner_state_version=version,
                        runner_state=state,
                        user_prompt=prompt,
                        config=make_config(base_url),
                    ),
                    id=f"{queue}-{version}",
                    task_queue=queue,
                    execution_timeout=timedelta(seconds=15),
                )
                == "done"
            )
    assert len(requests) == 2
    assert [(m["role"], m["content"]) for m in requests[1]["body"]["messages"]] == [
        ("user", "first"),
        ("assistant", "done"),
        ("user", "follow-up"),
    ]
    async with runner_database.sessions.begin() as db:
        state, version = await SessionRepository(db).read_runner_state(runner_database.session_id)
        entries = await AgentRepository(db).read_history_entries(runner_database.session_id)
    assert state is not None and version == 2
    messages = ModelMessagesTypeAdapter.validate_json(state)
    assert [m.metadata for m in messages] == [
        {"seq": seq, "authoritative": True} for seq in range(4)
    ]
    assert [entry.seq for entry in entries] == list(range(4))
    # History normalizes usage into token columns; runner_state retains SDK usage.
    assert ModelMessagesTypeAdapter.dump_python(
        [entry.message for entry in entries], exclude={"__all__": {"usage"}}
    ) == ModelMessagesTypeAdapter.dump_python(messages, exclude={"__all__": {"usage"}})


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["not-json", '{"history": []}', '[{"kind": "invalid"}]'])
async def test_invalid_state_fails_workflow_before_model_call(endpoint, runner_database, state):
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    from kapy.agent_runner.repository import AgentRepository

    base_url, requests = endpoint
    saver = RunnerActivities(runner_database.sessions, runner_database.outputs)
    client = await connect_temporal(runner_database.settings)
    queue = f"runner-invalid-state-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[saver.record_messages, saver.save_runner_state],
        plugins=[AgentPlugin(agent)],
    ):
        with pytest.raises(WorkflowFailureError) as error:
            await client.execute_workflow(
                RunnerWorkflow.run,
                RunnerInput(
                    session_id=runner_database.session_id,
                    runner_state_version=0,
                    runner_state=state,
                    user_prompt="hello",
                    config=make_config(base_url),
                ),
                id=queue,
                task_queue=queue,
                execution_timeout=timedelta(seconds=15),
            )
    assert isinstance(error.value.cause, ApplicationError)
    assert error.value.cause.type == "UserError"
    assert requests == []
    async with runner_database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(runner_database.session_id) == (
            None,
            0,
        )
        assert await AgentRepository(db).read_history_entries(runner_database.session_id) == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_activity_lost_ack_retries_same_batch_and_replays(
    endpoint, runner_database, monkeypatch
):
    from temporalio.exceptions import ApplicationError

    from kapy.agent_runner.repository import AgentRepository

    base_url, requests = endpoint
    saver = RunnerActivities(runner_database.sessions, runner_database.outputs)
    inputs = []
    broadcasts = []
    publish = runner_database.outputs._publish

    async def observe_publish(session_id, payload):
        await publish(session_id, payload)
        broadcasts.extend(TypeAdapter(list[OutputEvent]).validate_json(payload))

    monkeypatch.setattr(runner_database.outputs, "_publish", observe_publish)

    @activity.defn(name="kapy.record_history")
    async def lose_first_ack(data: MessageBatch) -> None:
        inputs.append(data)
        await saver.record_messages(data)
        if activity.info().attempt == 1:
            raise ApplicationError("simulated history commit with lost acknowledgment")

    client = await connect_temporal(runner_database.settings)
    queue = f"runner-history-retry-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[lose_first_ack, saver.save_runner_state],
        plugins=[AgentPlugin(agent)],
    ):
        handle = await client.start_workflow(
            RunnerWorkflow.run,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state_version=0,
                runner_state=None,
                user_prompt="hello",
                config=make_config(base_url),
            ),
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(seconds=30),
        )
        assert await handle.result() == "done"
        history = await handle.fetch_history()
    assert len(requests) == 1
    assert len(inputs) == 4 and inputs[0] == inputs[1] and inputs[2] == inputs[3]
    assert [(m.seq, m.authoritative) for m in inputs[0].messages] == [(0, False)]
    assert [(m.seq, m.authoritative) for m in inputs[2].messages] == [(0, True), (1, True)]
    async with runner_database.sessions.begin() as db:
        entries = await AgentRepository(db).read_history_entries(runner_database.session_id)
        state, version = await SessionRepository(db).read_runner_state(runner_database.session_id)
    assert [entry.seq for entry in entries] == [0, 1]
    assert state is not None and version == 1
    assert [m.metadata for m in ModelMessagesTypeAdapter.validate_json(state)] == [
        {"seq": 0, "authoritative": True},
        {"seq": 1, "authoritative": True},
    ]
    assert [
        (e.message.seq, e.message.authoritative)
        for e in broadcasts
        if isinstance(e, MessageCommitted)
    ] == [(0, False), (0, False), (0, True), (1, True)]
    published_before_replay = list(broadcasts)
    await Replayer(workflows=[RunnerWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
        history
    )
    assert len(inputs) == 4 and len(requests) == 1
    assert broadcasts == published_before_replay


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
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"), plugins=[PydanticAIPlugin()]
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
        context_plugin={"name": "not-installed", "config": {}},
    )
    path = tmp_path / "telegram.sqlite3"
    await migrate(path, "upgrade")
    activities = RunnerActivities(runner_database.sessions, runner_database.outputs)
    async with (
        open_storage(path) as storage,
        Worker(
            temporal,
            task_queue=queue,
            workflows=[RunnerWorkflow],
            activities=[activities.record_messages, activities.save_runner_state],
            plugins=[AgentPlugin(agent)],
        ),
    ):
        repository = TelegramRepository(async_sessionmaker(storage, expire_on_commit=False))
        telegram = AsyncMock(spec=TelegramClient)
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
        first = temporal.get_workflow_handle(f"kapy-runner:{session_id}")
        assert await asyncio.wait_for(first.result(), 30) == "done"
        second = await sessions.start_durable_runner(session_id, user_prompt="second prompt")
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
        assert not await sessions.has_legacy_checkpoint(session_id)
        assert not await sessions.is_durable_runner_running(session_id)
        assert not await sessions.read_inputs(session_id, "queued")
        (row,) = await repository.deliveries(42)
        follower = asyncio.create_task(
            TelegramDelivery(telegram, sessions, repository, 42).consume(row)
        )
        try:
            async with asyncio.timeout(5):
                while (await repository.get_delivery(delivery_key(row))).after_seq < 3:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            assert [
                call.args[2]
                for call in telegram.send.call_args_list
                if "draft_id" not in call.kwargs
            ] == ["done", "done"]
        finally:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
