"""Shell scope cleanup uses the real SDK request/error and client-close contracts."""

import json
import os
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from pydantic import ValidationError
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage
from shellctl import ShellctlClient, ShellctlClientError
from shellctl_fixture import shell_server as shell_server

from kapy.layers.scopes import retry_scope
from kapy.layers.shell import layer, runtime
from pydantic_ai_layer_protocol import LayerData, LayerRegistry, RetryableScopeError
from pydantic_ai_layer_protocol.registry import dump_object
from pydantic_ai_layer_protocol.types import JsonObject

pytestmark = pytest.mark.asyncio


def get_layers(deps: dict[str, LayerData]) -> dict[str, LayerData]:
    return deps


@pytest.fixture
def remote(monkeypatch):
    requests, responses, closed = [], [], []

    async def respond(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response
        return httpx2.Response(status, json=payload)

    class Client(ShellctlClient):
        def __init__(self, base_url, **kwargs):
            super().__init__(base_url, transport=httpx2.MockTransport(respond), **kwargs)

        async def close(self) -> None:
            await super().close()
            closed.append(self)

    monkeypatch.setattr(runtime, "open_client", partial(runtime.open_client, client_type=Client))
    return requests, responses, closed


async def test_enter_allocates_nothing_and_exit_forgets_only_confirmed_deletions(remote):
    requests, responses, closed = remote
    conversation_id, config = "shell-conversation", layer.Config(cwd="/remote/work")
    state = await layer.enter_conversation(conversation_id, config)
    assert state == layer.State(jobs={}) and not requests and not closed
    await layer.exit_conversation(conversation_id, config, None)
    assert not requests and not closed
    state.jobs = {"a": 2, "b": 3, "c": 4}
    responses.extend(
        [
            (200, {"job_id": "a", "deleted": True}),
            (404, {"error": {"code": "job_not_found", "message": "already deleted"}}),
            httpx2.ConnectError("lost acknowledgement"),
        ]
    )
    with pytest.raises(RetryableScopeError) as raised:
        await layer.exit_conversation(conversation_id, config, state)
    assert isinstance(raised.value.__cause__, httpx2.ConnectError)
    assert state.jobs == {"c": 4} and len(closed) == 1
    assert [request.method for request in requests] == ["DELETE"] * 3
    assert [request.url.path for request in requests] == [f"/v1/jobs/{job}" for job in "abc"]
    assert all(
        dict(request.url.params) == {"force": "true", "grace_seconds": "0"} for request in requests
    )
    responses.append((404, {"error": {"code": "job_not_found", "message": "already deleted"}}))
    await layer.exit_conversation(conversation_id, config, state)
    assert not state.jobs and len(closed) == 2
    await layer.exit_conversation(conversation_id, config, state)
    assert len(requests) == 4 and len(closed) == 2


async def test_old_identity_state_fails_layer_selection_before_io(remote):
    requests, _, closed = remote
    saved: JsonObject = {"scope_id": str(uuid4()), "jobs": {"known": 5}}
    with pytest.raises(ValidationError, match="scope_id"):
        layer.State.model_validate(saved)
    layers = LayerRegistry({"shell": layer.make}, get_layers=get_layers)
    agent = Agent(
        FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("unused")])),
        deps_type=dict[str, LayerData],
        capabilities=[layers],
    )
    with pytest.raises(UserError, match="Invalid layer configuration or state"):
        await agent.run("must reject", deps={"shell": {"config": {"cwd": "/tmp"}, "state": saved}})
    assert not requests and not closed


@pytest.mark.parametrize("conversation_id", [None, ""])
async def test_shell_tools_reject_missing_identity_before_remote_io(remote, conversation_id):
    requests, _, closed = remote
    definition = layer.make("shell", lambda deps: deps["shell"])
    # A normal Agent supplies an ID; a direct native toolset caller may omit it.
    ctx = RunContext(
        deps={"shell": {"config": {"cwd": "/tmp"}, "state": {"jobs": {}}}},
        model=TestModel(),
        usage=RunUsage(),
        conversation_id=conversation_id,
    )
    toolset = definition.capability.get_toolset()
    assert isinstance(toolset, FunctionToolset)
    tools = await toolset.get_tools(ctx)
    with pytest.raises(UserError, match="nonempty conversation_id"):
        await toolset.call_tool(
            "builtin_shell_run", {"script": "true"}, ctx, tools["builtin_shell_run"]
        )
    assert not requests and not closed


@pytest.mark.parametrize("key", ["KAPY_SESSION_ID", "KAPY_PLUGIN_PROVIDER", "KAPY_PLUGIN_NAME"])
async def test_config_cannot_override_resource_ownership(key):
    with pytest.raises(ValidationError, match="reserved ownership metadata"):
        layer.Config(cwd="/tmp", env={key: "model-chosen"})


@pytest.mark.skipif(
    "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
    reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with tmux and shellctl binaries",
)
async def test_ordinary_agent_uses_opaque_deps_and_conversation_identity_across_job_updates(
    shell_server,
):
    client, cwd = shell_server
    conversation_id = "standalone-shell-conversation"
    layers = LayerRegistry({"opaque-shell": layer.make}, get_layers=get_layers)
    definition = layers.definitions["opaque-shell"]
    assert definition.enter_conversation is not None
    config = layer.Config(cwd=str(cwd), base_url=client.base_url)
    initial = await definition.enter_conversation(conversation_id, config)
    # A plain dict has neither session_id nor layers attributes. Access is its sole adapter.
    deps = layers.load(
        {"opaque-shell": dump_object(config)}, {"opaque-shell": dump_object(initial)}
    )

    def metadata(observation):
        return json.loads(observation.split("<metadata>\n", 1)[1].split("\n</metadata>", 1)[0])

    cursors = []

    def model(messages, info):
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        assert {tool.name for tool in info.function_tools} == {
            "builtin_shell_run",
            "builtin_shell_wait",
            "builtin_shell_input",
            "builtin_shell_interrupt",
        }
        assert all(
            "conversation_id" not in tool.parameters_json_schema["properties"]
            for tool in info.function_tools
        )
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "builtin_shell_run",
                        {
                            "script": "printf 'scope:%s\\n' \"$KAPY_SESSION_ID\"; "
                            "read -r answer; printf 'echo:%s\\n' \"$answer\"; sleep 30",
                            "timeout": 0.2,
                        },
                    )
                ]
            )
        job_id = metadata(str(returns[0].content))["job_id"]
        if len(returns) in {1, 2}:
            cursors.append(layer.State.model_validate(deps["opaque-shell"]["state"]).jobs[job_id])
        steps = [
            ("builtin_shell_input", {"job_id": job_id, "text": "resume\n", "timeout": 0.2}),
            ("builtin_shell_wait", {"job_id": job_id, "timeout": 0}),
            ("builtin_shell_interrupt", {"job_id": job_id, "grace_seconds": 0}),
        ]
        if len(returns) <= len(steps):
            name, args = steps[len(returns) - 1]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model),
        deps_type=dict[str, LayerData],
        capabilities=[layers],
    )
    result = await agent.run("shell", deps=deps, conversation_id=conversation_id)
    observations = [
        str(part.content)
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert f"scope:{conversation_id}" in observations[0]
    assert "echo:resume" in observations[1]
    first = metadata(observations[0])
    job_id = first["job_id"]
    saved = layer.State.model_validate(layers.snapshot(deps)["opaque-shell"])
    assert "scope_id" not in layers.snapshot(deps)["opaque-shell"]
    assert saved.jobs[job_id] >= cursors[1] > cursors[0]
    ownership = json.loads((Path(first["output_path"]).parent / ".job-env.json").read_text())
    assert ownership == {
        "KAPY_SESSION_ID": conversation_id,
        "KAPY_PLUGIN_PROVIDER": "builtin",
        "KAPY_PLUGIN_NAME": "shell",
    }
    await client.delete(job_id, force=True, grace_seconds=0)

    def missing_model(messages, info):
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("missing")])
        return ModelResponse(
            parts=[ToolCallPart("builtin_shell_wait", {"job_id": job_id, "timeout": 0})]
        )

    await agent.run(
        "expired", deps=deps, model=FunctionModel(missing_model), conversation_id=conversation_id
    )
    assert layers.snapshot(deps)["opaque-shell"] == {
        "jobs": {},
    }
    assert definition.exit_conversation is not None
    await definition.exit_conversation(
        conversation_id, config, layer.State.model_validate(deps["opaque-shell"]["state"])
    )


@pytest.mark.parametrize(
    "failure,retryable",
    [
        (httpx2.ReadTimeout("timeout"), True),
        (httpx2.ConnectError("connection unavailable"), True),
        (httpx2.RemoteProtocolError("remote disconnected"), True),
        (httpx2.LocalProtocolError("invalid local request"), False),
        (httpx2.UnsupportedProtocol("unsupported local URL scheme"), False),
        ((429, "rate_limited"), True),
        ((502, "gateway"), True),
        ((503, "unavailable"), True),
        ((504, "gateway_timeout"), True),
        ((400, "invalid_request"), False),
        ((401, "unauthorized"), False),
        ((403, "forbidden"), False),
        ((500, "internal_error"), False),
        ((200, "invalid_payload"), False),
    ],
)
async def test_only_definite_temporary_shell_failures_opt_into_retry(remote, failure, retryable):
    _, responses, closed = remote
    if isinstance(failure, Exception):
        responses.append(failure)
    else:
        status, code = failure
        responses.append(
            (status, ["invalid"])
            if code == "invalid_payload"
            else (status, {"error": {"code": code, "message": "upstream failure"}})
        )
    conversation_id = "cleanup-conversation"
    state = layer.State(jobs={"known": 5})
    expected = (
        RetryableScopeError
        if retryable
        else type(failure)
        if isinstance(failure, Exception)
        else ShellctlClientError
    )
    with pytest.raises(expected):
        await layer.exit_conversation(conversation_id, layer.Config(cwd="/remote/work"), state)
    assert state.jobs == {"known": 5} and len(closed) == 1


@pytest.mark.skipif(
    "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
    reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with tmux and shellctl binaries",
)
async def test_invalid_token_protocol_error_from_real_sdk_does_not_retry(shell_server, monkeypatch):
    client, cwd = shell_server
    monkeypatch.setenv("SCOPE_INVALID_TOKEN", "invalid\nheader")
    config = layer.Config(cwd=str(cwd), base_url=client.base_url, token_env="SCOPE_INVALID_TOKEN")
    conversation_id, calls = "invalid-token-conversation", []
    state = layer.State(jobs={"known": 0})

    async def exit_conversation():
        calls.append(conversation_id)
        await layer.exit_conversation(conversation_id, config, state)

    with pytest.raises(httpx2.LocalProtocolError):
        await retry_scope(exit_conversation)
    assert calls == [conversation_id] and state.jobs == {"known": 0}
