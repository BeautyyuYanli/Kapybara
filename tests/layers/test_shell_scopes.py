"""Shell scope cleanup uses the real SDK request/error and client-close contracts."""

import json
import os
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from shellctl import ShellctlClient, ShellctlClientError
from shellctl_fixture import shell_server as shell_server

from kapy.agent_plugins.builtin.shell import ShellPluginState
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
    scope_id, config = uuid4(), layer.Config(cwd="/remote/work")
    state = await layer.enter_scope(scope_id, config)
    assert state == layer.State(scope_id=scope_id) and not requests and not closed
    await layer.exit_scope(scope_id, config, None)
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
        await layer.exit_scope(scope_id, config, state)
    assert isinstance(raised.value.__cause__, httpx2.ConnectError)
    assert state.scope_id == scope_id and state.jobs == {"c": 4} and len(closed) == 1
    assert [request.method for request in requests] == ["DELETE"] * 3
    assert [request.url.path for request in requests] == [f"/v1/jobs/{job}" for job in "abc"]
    assert all(
        dict(request.url.params) == {"force": "true", "grace_seconds": "0"} for request in requests
    )
    responses.append((404, {"error": {"code": "job_not_found", "message": "already deleted"}}))
    await layer.exit_scope(scope_id, config, state)
    assert not state.jobs and len(closed) == 2
    await layer.exit_scope(scope_id, config, state)
    assert len(requests) == 4 and len(closed) == 2


async def test_old_jobs_only_state_is_legacy_only_and_fails_layer_selection_before_io(remote):
    requests, _, closed = remote
    saved: JsonObject = {"jobs": {"known": 5}}
    assert ShellPluginState.model_validate(saved).model_dump(mode="json") == saved
    assert ShellPluginState().model_dump(mode="json") == {"jobs": {}}
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


@pytest.mark.skipif(
    "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
    reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with tmux and shellctl binaries",
)
async def test_ordinary_agent_uses_opaque_deps_and_preserves_scope_across_job_updates(shell_server):
    client, cwd = shell_server
    scope_id = uuid4()
    layers = LayerRegistry({"opaque-shell": layer.make}, get_layers=get_layers)
    definition = layers.definitions["opaque-shell"]
    assert definition.enter_scope is not None
    config = layer.Config(cwd=str(cwd), base_url=client.base_url)
    initial = await definition.enter_scope(scope_id, config)
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
    result = await agent.run("shell", deps=deps)
    observations = [
        str(part.content)
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert f"scope:{scope_id}" in observations[0]
    assert "echo:resume" in observations[1]
    first = metadata(observations[0])
    job_id = first["job_id"]
    saved = layer.State.model_validate(layers.snapshot(deps)["opaque-shell"])
    assert saved.scope_id == scope_id
    assert saved.jobs[job_id] >= cursors[1] > cursors[0]
    ownership = json.loads((Path(first["output_path"]).parent / ".job-env.json").read_text())
    assert ownership == {
        "KAPY_SESSION_ID": str(scope_id),
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

    await agent.run("expired", deps=deps, model=FunctionModel(missing_model))
    assert layers.snapshot(deps)["opaque-shell"] == {
        "scope_id": str(scope_id),
        "jobs": {},
    }
    assert definition.exit_scope is not None
    await definition.exit_scope(
        scope_id, config, layer.State.model_validate(deps["opaque-shell"]["state"])
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
    scope_id = uuid4()
    state = layer.State(scope_id=scope_id, jobs={"known": 5})
    expected = (
        RetryableScopeError
        if retryable
        else type(failure)
        if isinstance(failure, Exception)
        else ShellctlClientError
    )
    with pytest.raises(expected):
        await layer.exit_scope(scope_id, layer.Config(cwd="/remote/work"), state)
    assert state.jobs == {"known": 5} and len(closed) == 1


@pytest.mark.skipif(
    "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
    reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with tmux and shellctl binaries",
)
async def test_invalid_token_protocol_error_from_real_sdk_does_not_retry(shell_server, monkeypatch):
    client, cwd = shell_server
    monkeypatch.setenv("SCOPE_INVALID_TOKEN", "invalid\nheader")
    config = layer.Config(cwd=str(cwd), base_url=client.base_url, token_env="SCOPE_INVALID_TOKEN")
    scope_id, calls = uuid4(), []
    state = layer.State(scope_id=scope_id, jobs={"known": 0})

    async def exit_scope():
        calls.append(scope_id)
        await layer.exit_scope(scope_id, config, state)

    with pytest.raises(httpx2.LocalProtocolError):
        await retry_scope(exit_scope)
    assert calls == [scope_id] and state.jobs == {"known": 0}
