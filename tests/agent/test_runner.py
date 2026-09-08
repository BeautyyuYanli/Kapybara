import asyncio
import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import SecretStr

from kapy.agent import (
    AgentPayloadStore,
    ContextBudgetExceeded,
    OpenAICompatibleBackend,
    PayloadRef,
    Runner,
    RunnerConfig,
    apply_patch_plugin,
)
from kapy.agent.codec import DELTA_LIMIT, json_bytes
from kapy.rpc import MachineCaller
from kapy.state import (
    CheckpointWrite,
    OutputDelta,
    RecordPage,
    ReplyAddressPage,
    SessionInput,
    SessionView,
    WaitFor,
)


class Payloads:
    def __init__(self) -> None:
        self.data: dict[tuple[Any, str], bytes] = {}

    async def put(self, session_id: Any, data: bytes) -> PayloadRef:
        ref = PayloadRef(hashlib.sha256(data).hexdigest(), len(data))
        self.data[session_id, ref.sha256] = data
        return ref

    async def get(self, session_id: Any, ref: PayloadRef) -> bytes:
        return self.data[session_id, ref.sha256]


class Context:
    def __init__(self, state: Any) -> None:
        self.run_id = uuid4()
        now = datetime.now(UTC)
        self.session = SessionView(
            uuid4(), "test", ("machine",), "machine", {}, "running", self.run_id, "0", now, now
        )
        self.attempt, self.recovered = 1, False
        self.inputs: tuple[SessionInput, ...] = (
            SessionInput(uuid4(), 1, "queue", "Please help", None),
        )
        self.state, self.checkpoint_number = state, 0
        self.writes: list[CheckpointWrite] = []
        self.deltas: list[OutputDelta] = []
        self.steer: list[SessionInput] = []

    async def poll_steer(self, *, limit: int = 64) -> tuple[SessionInput, ...]:
        values = tuple(self.steer[:limit])
        del self.steer[:limit]
        return values

    async def checkpoint(self, write: CheckpointWrite) -> str:
        assert write.number == self.checkpoint_number + 1
        self.state, self.checkpoint_number = copy.deepcopy(write.state), write.number
        self.writes.append(copy.deepcopy(write))
        return str(write.number)

    async def unreplied_addresses(self, *, after: int = 0, limit: int = 64) -> ReplyAddressPage:
        consumed = {i for write in self.writes for i in write.consumed_input_ids}
        items = [i for i in self.inputs if i.id in consumed and i.being_waited_id and i.seq > after]
        items.sort(key=lambda i: i.seq)
        return ReplyAddressPage(
            tuple(i.being_waited_id for i in items[:limit] if i.being_waited_id is not None),
            items[limit - 1].seq if len(items) > limit else None,
        )

    async def emit(self, delta: OutputDelta) -> str:
        assert len(json_bytes(delta.data)) < DELTA_LIMIT
        self.deltas.append(delta)
        return str(len(self.deltas))

    async def read_history(self, *, after: str | None = None, limit: int = 200) -> RecordPage:
        return RecordPage((), "0", False)


class Caller:
    def __init__(self, media: bytes = b"\x89PNG\r\n\x1a\nimage") -> None:
        self.calls: list[tuple[str, str, dict[str, Any], float]] = []
        self.media = media

    async def call(
        self,
        machine_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60.0,  # noqa: ASYNC109
    ) -> Any:
        self.calls.append((machine_id, method, params, timeout))
        if method == "process.list":
            return {"items": [], "next": None}
        if method == "file.pull":
            return {"size": len(self.media), "state": "open", "sha256": None, "offset": 0}
        if method == "file.chunk":
            start = params["offset"]
            content = self.media[start : start + params["max_bytes"]]
            return {
                "data_base64": base64.b64encode(content).decode(),
                "start": start,
                "next": start + len(content),
                "available": len(self.media),
                "eof": start + len(content) == len(self.media),
                "truncated": False,
            }
        if method == "file.finish":
            return {"size": len(self.media), "state": "complete", "sha256": None}
        if method == "file.abort":
            return {"aborted": True}
        raise AssertionError(method)


async def authorize(session_id: Any, channels: Any) -> None:
    pass


def response(
    *,
    text: str | None = None,
    name: str | None = None,
    args: dict[str, Any] | None = None,
    call_id: str = "call1",
    tokens: int = 100,
) -> httpx2.Response:
    delta: dict[str, Any] = {"role": "assistant"}
    if text is not None:
        delta["content"] = text
    if name is not None:
        delta["tool_calls"] = [
            {
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args or {})},
            }
        ]
    chunks = [
        {
            "id": "resp",
            "object": "chat.completion.chunk",
            "model": "gpt-5.6-luna",
            "created": 1,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": "resp",
            "object": "chat.completion.chunk",
            "model": "gpt-5.6-luna",
            "created": 1,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls" if name else "stop"}
            ],
            "usage": {
                "prompt_tokens": tokens,
                "completion_tokens": 10,
                "total_tokens": tokens + 10,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def process_update(params: dict[str, Any]) -> dict[str, Any]:
    chunk = {
        "data_base64": "",
        "start": 0,
        "next": 0,
        "available": 0,
        "truncated": False,
        "eof": True,
    }
    return {
        "process": {
            "process_id": params["process_id"],
            "state": "exited",
            "cwd": "/session",
            "mode": "stdio",
            "exit_code": 0,
            "output_complete": True,
            "error": None,
        },
        "reason": "exited",
        "output": {"kind": "stdio", "stdout": chunk, "stderr": chunk},
    }


def runner(
    client: httpx2.AsyncClient, caller: Caller | MachineCaller | None = None, **kwargs: Any
) -> Runner:
    kwargs.setdefault("plugins", (apply_patch_plugin(),))
    return Runner(
        RunnerConfig(1_000_000),
        caller or Caller(),
        model_backend=OpenAICompatibleBackend(
            base_url="https://model.invalid/v1", api_key=SecretStr("dummy-key"), http_client=client
        ),
        payload_store=cast(AgentPayloadStore, Payloads()),
        authorize_wait=authorize,
        **kwargs,
    )  # type: ignore[bad-argument-type]


@pytest.mark.asyncio
async def test_stream_tool_roundtrip_archives_messages_and_final_is_uncommitted() -> None:
    requests = []
    caller = Caller()

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(name="process_list")
        return response(text="All done")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, caller)
        ctx = Context(agent.initial_state(instructions="Be precise", skills=[]))
        result = await agent(ctx)
    assert result.output == "All done"
    assert result.checkpoint.number == ctx.checkpoint_number + 1
    assert result.checkpoint not in ctx.writes
    assert len(requests) == 2
    assert caller.calls[0][1] == "process.list"
    assert caller.calls[0][3] == 60.0
    assert caller.calls[0][2] == {"session_id": str(ctx.session.id), "limit": 50}
    assert sum(len(w.consumed_input_ids) for w in ctx.writes) == 1
    archived = [message for write in ctx.writes for message in write.messages]
    ids = [message.message_id for message in archived]
    assert len(ids) == len(set(ids))
    assert [message.kind for message in archived] == [
        "model_request",
        "model_response",
        "model_request",
        "model_response",
    ]
    usage = result.checkpoint.state.data["last_usage"]
    assert isinstance(usage, dict)
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 10
    assert usage["cache_read_tokens"] == 20
    assert any(delta.kind == "text_delta" for delta in ctx.deltas)
    result_delta = next(delta for delta in ctx.deltas if delta.kind == "tool_result")
    assert isinstance(result_delta.data, dict) and result_delta.data["name"] == "process_list"


@pytest.mark.asyncio
async def test_stream_media_rejection_retries_text_without_rereading() -> None:
    requests = []
    caller = Caller()

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(name="read_media", args={"path": "image.png"})
        if len(requests) == 2:
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_image",
                        "message": "Unsupported image format; token dummy-key",
                    }
                },
            )
        return response(text="I can continue with text")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, caller)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        result = await agent(ctx)
    assert result.output == "I can continue with text"
    assert len(requests) == 3
    assert sum(call[1] == "file.pull" for call in caller.calls) == 1
    second, third = json.dumps(requests[1]), json.dumps(requests[2])
    assert "data:image/png;base64," in second
    assert "data:image/png;base64," not in third
    assert "call1" in third and "rejected" in third
    assert "dummy-key" not in third
    assert result.checkpoint.state.data["media_fallback_call_ids"] == ["call1"]
    raw = [m for w in ctx.writes for m in w.messages if m.kind == "model_request"]
    assert any("kapy_media_refs" in json.dumps(m.data) for m in raw)
    assert any(delta.kind == "notice" for delta in ctx.deltas)


@pytest.mark.asyncio
async def test_model_snapshot_and_reserved_steer_at_natural_end() -> None:
    requests = []
    ctx = None

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            assert ctx is not None
            ctx.steer.append(SessionInput(uuid4(), 2, "steer", "Also handle this", None))
        return response(text=f"Reply {len(requests)}")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="Original instruction", skills=[]))
        agent.config = replace(agent.config, model="custom-model")
        result = await agent(ctx)
    assert len(requests) == 2
    assert requests[0]["model"] == requests[1]["model"] == "custom-model"
    assert "Also handle this" in json.dumps(requests[1])
    assert result.output == "Reply 2"
    assert sum(len(w.consumed_input_ids) for w in ctx.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("process_start", {"command": "echo allowed"}),
        ("process_write", {"process_id": "cfe7a27d-dfcf-412d-9ff8-93c650f5573d", "input": "once"}),
    ],
)
async def test_cancelled_tool_recovery_never_replays_write_or_start(
    tool_name: str, args: dict[str, Any]
) -> None:
    class InterruptedCaller(Caller):
        async def call(
            self,
            machine_id: str,
            method: str,
            params: dict[str, Any],
            *,
            timeout: float = 60.0,  # noqa: ASYNC109
        ) -> Any:
            self.calls.append((machine_id, method, params, timeout))
            if method in ("process.start", "process.write"):
                raise asyncio.CancelledError
            if method == "process.wait":
                return process_update(params)
            raise AssertionError(method)

    requests = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(name=tool_name, args=args)
        return response(text="Recovered")

    caller = InterruptedCaller()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, caller)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        with pytest.raises(asyncio.CancelledError):
            await agent(ctx)
        ctx.attempt, ctx.recovered = 2, True
        result = await agent(ctx)
    assert result.output == "Recovered"
    assert sum(call[1] == tool_name.replace("_", ".") for call in caller.calls) == 1
    if tool_name == "process_write":
        assert "outcome_unknown" in json.dumps(requests[-1])
    else:
        assert any(call[1] == "process.wait" for call in caller.calls)
    assert "call1" in json.dumps(requests[-1])


@pytest.mark.asyncio
async def test_wait_authorization() -> None:
    channel = str(uuid4())
    approved = []

    async def permit(session_id: Any, channels: Any) -> None:
        approved.append((session_id, channels))

    def handle(request: httpx2.Request) -> httpx2.Response:
        return response(text="Waiting now", name="wait_for", args={"ids": [channel]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        agent.authorize_wait = permit
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        result = await agent(ctx)
    assert result.output == WaitFor((UUID(channel),))
    assert len(approved) == 1


@pytest.mark.asyncio
async def test_rejected_wait_is_correctable_and_large_media_stays_text() -> None:
    requests = []

    async def deny(session_id: Any, channels: Any) -> None:
        raise PermissionError("Channel is not authorized")

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(name="wait_for", args={"ids": [str(uuid4())]})
        if len(requests) == 2:
            return response(name="read_media", args={"path": "large.png"}, call_id="media2")
        return response(text="Handled both errors")

    caller = Caller(b"x" * (20 * 1024 * 1024 + 1))
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client, caller)
        agent.authorize_wait = deny
        result = await agent(Context(agent.initial_state(instructions="", skills=[])))
    assert result.output == "Handled both errors"
    assert "not authorized" in json.dumps(requests[1])
    assert "exceeds" in json.dumps(requests[2])
    assert not any(call[1] == "file.chunk" for call in caller.calls)
    assert any(call[1] == "file.abort" for call in caller.calls)


@pytest.mark.asyncio
async def test_explicit_context_rejection_is_bounded_across_recovery() -> None:
    requests = []

    def reject(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(
            400,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "maximum context length exceeded",
                }
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reject)) as client:
        agent = runner(client)
        initial = agent.initial_state(instructions="fixed", skills=[])
        cycles: list[dict[str, Any]] = []
        for index in range(10):
            cycles.append(
                {
                    "turn_id": str(index),
                    "closed": True,
                    "level": 0,
                    "inputs": [f"old input {index}"],
                    "output": "old answer",
                    "messages": [
                        {
                            "kind": "request",
                            "parts": [
                                {"part_kind": "user-prompt", "content": f"old input {index}"}
                            ],
                        },
                        {
                            "kind": "response",
                            "parts": [{"part_kind": "text", "content": "old answer"}],
                        },
                    ],
                }
            )
        initial.data["cycles"] = cast(Any, cycles)
        ctx = Context(initial)
        with pytest.raises(ContextBudgetExceeded):
            await agent(ctx)
        assert len(requests) == 3
        assert ctx.state.data["context_retries"] == 2
        ctx.attempt, ctx.recovered = 2, True
        with pytest.raises(ContextBudgetExceeded):
            await agent(ctx)
        assert len(requests) == 4
        assert ctx.state.data["context_retries"] == 2


@pytest.mark.asyncio
async def test_authentication_error_does_not_trigger_media_or_context_retries() -> None:
    requests = []

    def reject(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            401, json={"error": {"message": "Invalid API key for image service"}}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reject)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        with pytest.raises(Exception) as caught:
            await agent(ctx)
    assert getattr(caught.value, "status_code", None) == 401
    assert len(requests) == 1
    assert not any(delta.kind == "notice" for delta in ctx.deltas)
