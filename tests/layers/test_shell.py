"""The shell layer borrows one local client and persists only remote job cursors."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import anyio
import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from shellctl import ShellctlClient
from shellctl_fixture import shell_server as shell_server

from kapy.layers.shell import layer, runtime
from pydantic_ai_layer_protocol import LayerDeps, LayerRegistry
from pydantic_ai_layer_protocol.types import JsonObject

pytestmark = pytest.mark.asyncio

layers = LayerRegistry[LayerDeps](
    {"shell": layer.make},
    get_layers=lambda deps: deps.layers,
    get_resources=lambda deps: deps.resources,
)


def returns(messages):
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


async def test_shell_tools_reuse_one_client_and_forward_only_configured_env(monkeypatch):
    requests, clients, closed = [], [], []

    async def respond(request):
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "job_id": "job",
                "done": False,
                "status": "running",
                "output_path": "/remote/job.log",
                "output": "test-bearer configured-secret",
                "offset": 10 * len(requests),
                "truncated": False,
            },
        )

    class Client(ShellctlClient):
        def __init__(self, base_url, **kwargs):
            super().__init__(base_url, transport=httpx2.MockTransport(respond), **kwargs)
            clients.append(self)

        async def close(self) -> None:
            await super().close()
            closed.append(self)

    monkeypatch.setattr(runtime, "ShellctlClient", Client)
    monkeypatch.setenv("SHELL_TEST_TOKEN", "test-bearer")
    configs: dict[str, JsonObject] = {
        "shell": {
            "cwd": "/remote/work",
            "token_env": "SHELL_TEST_TOKEN",
            "redact_patterns": ["configured-secret"],
            "env": {"KAPY_SESSION_ID": "supplied-by-user"},
        }
    }
    deps = LayerDeps(layers=layers.load(configs, {}))

    def model(messages, info):
        observations = returns(messages)
        if not observations:
            return ModelResponse(parts=[ToolCallPart("builtin_shell_run", {"script": "true"})])
        assert "[REDACTED] [REDACTED]" in observations[-1]
        if len(observations) == 1:
            return ModelResponse(parts=[ToolCallPart("builtin_shell_wait", {"job_id": "job"})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model), deps_type=LayerDeps, capabilities=[layers])
    async with layers.open_conversation("opaque-id", configs) as resources:
        deps.resources = resources
        assert len(clients) == 1 and not requests
        assert (await agent.run("shell", deps=deps)).output == "done"
        assert not closed
    assert clients == closed
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v1/jobs/run"),
        ("POST", "/v1/jobs/job/wait"),
    ]
    assert all(request.headers["authorization"] == "Bearer test-bearer" for request in requests)
    assert json.loads(requests[0].content)["env"] == {"KAPY_SESSION_ID": "supplied-by-user"}
    assert json.loads(requests[1].content)["offset"] == 10
    assert layers.snapshot(deps) == {"shell": {"jobs": {"job": 20}}}


async def test_old_identity_state_fails_layer_selection_before_io():
    saved: JsonObject = {"scope_id": str(uuid4()), "jobs": {"known": 5}}
    deps = LayerDeps(layers={"shell": {"config": {"cwd": "/tmp"}, "state": saved}})

    def model(messages, info):
        pytest.fail("Invalid saved State must fail before the model is called")

    agent = Agent(FunctionModel(model), deps_type=LayerDeps, capabilities=[layers])
    with pytest.raises(UserError, match="Invalid layer configuration or state"):
        await agent.run("must reject", deps=deps)


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel-and-timeout"])
async def test_shell_close_joins_timed_out_task(monkeypatch, cancel):
    started, release_close, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    resource = await runtime.enter_conversation("test", layer.Config(cwd="/tmp"))
    original_close = resource.client.close

    async def stalled_close() -> None:
        # The HTTP pool also uses an AnyIO shield while closing connections.
        with anyio.CancelScope(shield=True):
            started.set()
            try:
                await release_close.wait()
            finally:
                finished.set()

    monkeypatch.setattr(resource.client, "close", stalled_close)
    task = asyncio.create_task(runtime.exit_conversation(resource))
    try:
        await asyncio.wait_for(started.wait(), 5)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 7)
        assert task.done() and finished.is_set()
    finally:
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await original_close()


@pytest.mark.skipif(
    "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
    reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with tmux and shellctl binaries",
)
async def test_standalone_shell_keeps_remote_jobs_and_updates_cursors(shell_server):
    client, cwd = shell_server
    conversation_id = "standalone-shell-conversation"
    configs: dict[str, JsonObject] = {
        "shell": {
            "cwd": str(cwd),
            "base_url": client.base_url,
            "env": {"SHELL_LABEL": "standalone"},
        }
    }
    deps = LayerDeps(layers=layers.load(configs, {}))

    def metadata(observation):
        return json.loads(observation.split("<metadata>\n", 1)[1].split("\n</metadata>", 1)[0])

    cursors = []

    def model(messages, info):
        observations = returns(messages)
        if not observations:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "builtin_shell_run",
                        {
                            "script": "printf 'label:%s\\n' \"$SHELL_LABEL\"; "
                            "read -r answer; printf 'echo:%s\\n' \"$answer\"; sleep 30",
                            "timeout": 0.2,
                        },
                    )
                ]
            )
        job_id = metadata(observations[0])["job_id"]
        if len(observations) in {1, 2}:
            cursors.append(layer.State.model_validate(deps.layers["shell"]["state"]).jobs[job_id])
        steps = [
            ("builtin_shell_input", {"job_id": job_id, "text": "resume\n", "timeout": 0.2}),
            ("builtin_shell_wait", {"job_id": job_id, "timeout": 0}),
            ("builtin_shell_interrupt", {"job_id": job_id, "grace_seconds": 0}),
        ]
        if len(observations) <= len(steps):
            name, args = steps[len(observations) - 1]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model), deps_type=LayerDeps, capabilities=[layers])
    async with layers.open_conversation(conversation_id, configs) as resources:
        deps.resources = resources
        result = await agent.run("shell", deps=deps, conversation_id=conversation_id)
    observations = returns(result.all_messages())
    assert "label:standalone" in observations[0] and "echo:resume" in observations[1]
    first = metadata(observations[0])
    job_id = first["job_id"]
    saved = layer.State.model_validate(layers.snapshot(deps)["shell"])
    assert saved.jobs[job_id] >= cursors[1] > cursors[0]
    env = json.loads((Path(first["output_path"]).parent / ".job-env.json").read_text())
    assert env == {"SHELL_LABEL": "standalone"}
    assert (await client.status(job_id)).job_id == job_id
    await client.delete(job_id, force=True, grace_seconds=0)

    def missing_model(messages, info):
        if returns(messages):
            return ModelResponse(parts=[TextPart("missing")])
        return ModelResponse(
            parts=[ToolCallPart("builtin_shell_wait", {"job_id": job_id, "timeout": 0})]
        )

    async with layers.open_conversation(conversation_id, configs) as resources:
        deps.resources = resources
        await agent.run("expired", deps=deps, model=FunctionModel(missing_model))
    assert layers.snapshot(deps) == {"shell": {"jobs": {}}}
