"""Shell observations and failure ownership with the real SDK HTTP contract.

The final test additionally exercises real PostgreSQL, Go shellctl/tmux and the
application's native Pydantic AI capability wiring inside a disposable container.
"""

import asyncio
import json
import os
import socket
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import httpx2
import pytest
import pytest_asyncio
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import Tool
from shellctl import ShellctlClient, ShellctlClientError

from kapy.agent_plugins import AgentPluginService, PluginSpec, StateConflict
from kapy.agent_plugins.builtin.shell import ShellPlugin, ShellPluginConfig, ShellPluginState
from kapy.agent_plugins.builtin.shell import plugin as shell_module
from kapy.agent_plugins.contracts import SessionContext, VersionedState
from kapy.application.agent import create_execution_factory, create_registry
from kapy.control.sessions import CreateSession, SessionService
from kapy.lifecycle import LifecycleError, LifecycleStatus


class MemoryStore:
    """Independent values and real UUID CAS semantics, with one-shot fault injection."""

    def __init__(self, jobs: dict[str, int] | None = None) -> None:
        self.value = ShellPluginState(jobs=jobs) if jobs is not None else None
        self.revision = uuid4()
        self.before_replace: Callable[[ShellPluginState], Awaitable[None]] | None = None
        self.read_error: Exception | None = None

    async def read(self) -> VersionedState[ShellPluginState]:
        if self.read_error:
            raise self.read_error
        return VersionedState(
            self.revision, self.value.model_copy(deep=True) if self.value else None
        )

    async def replace(
        self, value: ShellPluginState, *, expected_revision: UUID
    ) -> VersionedState[ShellPluginState]:
        if self.before_replace:
            callback, self.before_replace = self.before_replace, None
            await callback(value)
        if expected_revision != self.revision:
            raise StateConflict()
        self.value, self.revision = value.model_copy(deep=True), uuid4()
        return await self.read()


def page(job_id="a", output="ready\n", *, offset=6, truncated=False):
    return {
        "job_id": job_id,
        "status": "running",
        "done": False,
        "exit_code": None,
        "output_path": f"/remote/jobs/{job_id}/output.log",
        "output": output,
        "offset": offset,
        "truncated": truncated,
    }


def metadata(observation: str):
    return json.loads(observation.split("<metadata>\n", 1)[1].split("\n</metadata>", 1)[0])


@pytest.fixture
def shell_mock(monkeypatch):
    requests: list[httpx2.Request] = []
    responses: list[dict | Exception] = []

    async def respond(request):
        requests.append(request)
        assert responses, f"Unexpected request: {request.method} {request.url}"
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx2.Response(404 if "error" in response else 200, json=response)

    def client(base_url, **kwargs):
        return ShellctlClient(base_url, transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr(shell_module, "ShellctlClient", client)
    store = MemoryStore()
    ctx = SessionContext(
        uuid4(),
        "builtin",
        "shell",
        ShellPluginConfig(cwd="/remote/work", env={"HELLO": "world"}),
        store,
    )
    return ctx, requests, responses


@pytest.mark.parametrize(
    "invalid",
    [
        {"cwd": "relative"},
        {"cwd": "/bad\x00"},
        {"base_url": "ftp://example.com"},
        {"base_url": "relative"},
        {"env": {"": "value"}},
        {"env": {"a=b": "value"}},
        {"env": {"A": "bad\x00"}},
        {"env": {"KAPY_SESSION_ID": "forged"}},
        {"token_env": ""},
        {"redact_patterns": ["["]},
        {"unknown": True},
    ],
)
def test_config_is_pure_and_rejects_invalid_remote_settings(invalid):
    with pytest.raises(ValidationError):
        ShellPluginConfig.model_validate({"cwd": "/need-not-exist", **invalid})
    config = ShellPluginConfig(cwd="/need-not-exist")
    assert ShellPluginConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.asyncio
async def test_sdk_mapping_ownership_retention_and_close(shell_mock):
    ctx, requests, responses = shell_mock
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        assert not requests and binding.instructions is not None
        assert "builtin_shell_input" in await binding.instructions()
        assert "Unknown job" in await tools["wait"]("other")
        assert not requests
        responses.append(page())
        result = await tools["run"]("printf ready", timeout=0.25)
        assert metadata(result)["job_id"] == "a"
        payload = json.loads(requests[0].content)
        assert payload["mode"] == "pty" and payload["cwd"] == "/remote/work"
        assert payload["output_limit"] == 8192 and payload["idle_flush_seconds"] == 0.5
        assert payload["env"] == {
            "HELLO": "world",
            "KAPY_SESSION_ID": str(ctx.session_id),
            "KAPY_PLUGIN_PROVIDER": "builtin",
            "KAPY_PLUGIN_NAME": "shell",
        }
    assert ctx.state.value.jobs == {"a": 6}
    assert [request.method for request in requests] == ["POST"]
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        responses.append(page(output="next", offset=10))
        await tools["input"]("a", "hello\n", timeout=1)
        assert json.loads(requests[-1].content)["offset"] == 6
        assert json.loads(requests[-1].content)["text"] == "hello\n"
        responses.append(page(output="", offset=10))
        await tools["wait"]("a", timeout=0)
        assert json.loads(requests[-1].content)["offset"] == 10
        responses.extend(
            [
                {
                    "job_id": "a",
                    "status": "terminated",
                    "done": True,
                    "created_at": "2026-09-22",
                    "offset": 10,
                },
                httpx2.ConnectError("tail unavailable"),
            ]
        )
        assert metadata(await tools["interrupt"]("a"))["status"] == "terminated"
        assert json.loads(requests[-2].content)["grace_seconds"] == 5
    assert ctx.state.value.jobs == {"a": 10}
    responses.append({"job_id": "a", "deleted": True})
    await ShellPlugin().close_session(ctx)
    assert not ctx.state.value.jobs
    assert requests[-1].method == "DELETE"
    assert dict(requests[-1].url.params) == {"force": "true", "grace_seconds": "0"}
    await ShellPlugin().close_session(ctx)
    assert not responses


@pytest.mark.asyncio
async def test_tool_validation_uses_native_schema_and_sdk_bounds(shell_mock):
    ctx, _, _ = shell_mock
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {declaration.name: Tool(declaration.function) for declaration in binding.tools}
        for name, args in (
            ("run", {"script": ""}),
            ("run", {"script": "echo", "timeout": 0}),
            ("run", {"script": "echo", "timeout": 301}),
            ("input", {"job_id": "a", "text": "", "timeout": 0}),
            ("wait", {"job_id": "a", "timeout": -1}),
            ("wait", {"job_id": "", "timeout": 0}),
            ("interrupt", {"job_id": "a", "grace_seconds": float("inf")}),
        ):
            with pytest.raises(ValidationError):
                tools[name].function_schema.validator.validate_python(args)
        assert (
            tools["wait"].function_schema.validator.validate_python({"job_id": "a", "timeout": 0})[
                "timeout"
            ]
            == 0
        )


@pytest.mark.asyncio
async def test_output_redaction_truncation_tail_cursor_and_fallback(shell_mock, monkeypatch):
    ctx, requests, responses = shell_mock
    monkeypatch.setenv("SHELLCTL_AUTH_TOKEN", "secret-token")
    ctx.config.redact_patterns = [r"password=\w+"]
    first_output = "secret-token password=hunter " + "中" * 2721
    responses.extend(
        [
            page(output=first_output, offset=len(first_output.encode("utf-8")), truncated=True),
            page(output="末" * 2730, offset=90000),
        ]
    )
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        observation = await tools["run"]("echo")
        assert "secret-token" not in observation and "hunter" not in observation
        assert "[REDACTED]" in observation and "truncated" in observation
        assert "�" not in observation and len(observation.encode()) < 8700
        assert "<output>\n[REDACTED] [REDACTED] 中中中" in observation
        assert observation.endswith("末" * 10 + "\n</output>")
        assert metadata(observation)["output_path"] == "/remote/jobs/a/output.log"
        assert "Full log: /remote/jobs/a/output.log" in observation
        assert ctx.state.value.jobs == {"a": 90000}
        assert requests[0].headers["authorization"] == "Bearer secret-token"
        assert "secret-token" not in requests[0].content.decode()
        responses.extend(
            [
                page(output="z" * 8192, offset=90000 + 8192, truncated=True),
                httpx2.ConnectError("tail unavailable"),
            ]
        )
        fallback = await tools["wait"]("a")
        assert "truncated" in fallback
        assert "<output>\n" + "z" * 4096 in fallback
        assert fallback.endswith("z" * 4096 + "\n</output>")
        assert ctx.state.value.jobs == {"a": 90000 + 8192}


@pytest.mark.asyncio
async def test_transport_failures_do_not_retry_or_forget_but_missing_does(shell_mock):
    ctx, requests, responses = shell_mock
    ctx.state.value = ShellPluginState(jobs={"a": 5, "b": 4})
    responses.extend(
        [
            httpx2.ReadTimeout("response lost"),
            {"error": {"code": "job_not_found", "message": "expired"}},
            httpx2.ReadTimeout("creation response lost"),
        ]
    )
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        assert "response lost" in await tools["input"]("a", "once\n")
        assert ctx.state.value.jobs == {"a": 5, "b": 4}
        assert "expired" in await tools["wait"]("a")
        assert ctx.state.value.jobs == {"b": 4}
        assert "may already" in await tools["run"]("echo")
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_cas_merges_without_repeating_side_effects_or_regressing_cursor(shell_mock):
    ctx, requests, responses = shell_mock

    async def competitor(_):
        ctx.state.value = ShellPluginState(jobs={"other": 12, "a": 90})
        ctx.state.revision = uuid4()

    ctx.state.before_replace = competitor
    responses.append(page(offset=6))
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        await tools["run"]("echo")
    assert ctx.state.value.jobs == {"a": 90, "other": 12}
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cursor_cas_does_not_resurrect_a_removed_job(shell_mock):
    ctx, requests, responses = shell_mock
    ctx.state.value = ShellPluginState(jobs={"a": 5, "other": 1})

    async def competitor(_):
        ctx.state.value = ShellPluginState(jobs={"other": 90})
        ctx.state.revision = uuid4()

    ctx.state.before_replace = competitor
    responses.append(page(offset=11))
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        await tools["wait"]("a")
    assert ctx.state.value.jobs == {"other": 90}
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_level_cancellation_shields_known_job_compensation(shell_mock):
    ctx, requests, responses = shell_mock
    responses.extend([page(), {"job_id": "a", "deleted": True}])
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        with anyio.CancelScope() as scope:

            async def cancel(_):
                scope.cancel()
                await anyio.lowlevel.checkpoint()

            ctx.state.before_replace = cancel
            await tools["run"]("echo")
        assert scope.cancelled_caught
    assert ctx.state.value is None
    assert [request.method for request in requests] == ["POST", "DELETE"]


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_original_error_and_known_job(shell_mock):
    ctx, _, responses = shell_mock
    responses.extend([page(), httpx2.ConnectError("cleanup unavailable")])

    async def reject(_):
        raise LifecycleError("closing")

    ctx.state.before_replace = reject
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        with pytest.raises(LifecycleError, match="closing") as raised:
            await tools["run"]("echo")
    notes = " ".join(raised.value.__notes__)
    assert "job a" in notes and "cleanup unavailable" in notes


@pytest.mark.asyncio
async def test_cursor_write_failure_retains_registered_job_without_http_retry(shell_mock):
    ctx, requests, responses = shell_mock
    responses.append(page())
    failure = OSError("cursor commit failed")

    async def fail_cursor(_):
        raise failure

    async def after_registration(_):
        ctx.state.before_replace = fail_cursor

    ctx.state.before_replace = after_registration
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        with pytest.raises(OSError) as raised:
            await tools["run"]("echo")
    assert raised.value is failure
    assert ctx.state.value.jobs == {"a": 0}
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v1/jobs/run")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["rejected", "uncommitted", "committed", "unknown", "cancelled"]
)
async def test_registration_failure_compensates_only_definitely_unregistered(shell_mock, failure):
    ctx, requests, responses = shell_mock
    responses.append(page())

    async def fail(value):
        if failure == "rejected":
            raise LifecycleError("closing")
        if failure == "committed":
            ctx.state.value = value.model_copy(deep=True)
        if failure == "unknown":
            ctx.state.read_error = OSError("database unreachable")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        raise OSError("commit response lost")

    ctx.state.before_replace = fail
    if failure in {"rejected", "uncommitted", "cancelled"}:
        responses.append({"job_id": "a", "deleted": True})
    async with ShellPlugin().open_execution(ctx) as binding:
        tools = {tool.name: tool.function for tool in binding.tools}
        with pytest.raises((LifecycleError, OSError, asyncio.CancelledError)) as raised:
            await tools["run"]("echo")
    assert "job a" in " ".join(raised.value.__notes__)
    assert len(requests) == (2 if failure in {"rejected", "uncommitted", "cancelled"} else 1)
    if failure == "committed":
        assert ctx.state.value.jobs == {"a": 0}


@pytest.mark.asyncio
async def test_close_retains_partial_progress_and_not_found_is_idempotent(shell_mock):
    ctx, requests, responses = shell_mock
    ctx.state.value = ShellPluginState(jobs={"c": 10, "b": 20, "a": 30})
    responses.extend(
        [
            {"job_id": "a", "deleted": True},
            httpx2.ConnectError("unavailable"),
        ]
    )
    with pytest.raises(httpx2.ConnectError):
        await ShellPlugin().close_session(ctx)
    assert ctx.state.value.jobs == {"b": 20, "c": 10}
    responses.extend(
        [
            {"error": {"code": "job_not_found", "message": "already deleted"}},
            {"job_id": "c", "deleted": True},
        ]
    )
    await ShellPlugin().close_session(ctx)
    assert not ctx.state.value.jobs
    assert [request.url.path for request in requests] == [
        "/v1/jobs/a",
        "/v1/jobs/b",
        "/v1/jobs/b",
        "/v1/jobs/c",
    ]


@pytest_asyncio.fixture
async def shell_server(tmp_path):
    """A private server, SQLite directory and tmux socket; never the deployment."""
    binaries = Path(os.environ["KAPY_SHELLCTL_TEST_BIN_DIR"])
    assert (binaries / "shellctl").is_file(), "Run make -C packages/shellctl build-server"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LANG") if key in os.environ}
    # The application image intentionally uses nologin; tmux needs a real shell.
    environment.update(
        PATH=f"{binaries}:/usr/bin:/bin", XDG_DATA_HOME=str(tmp_path), SHELL="/bin/bash"
    )
    tmux_socket = tmp_path / "shellctl/runtime/tmux.sock"
    with (tmp_path / "server.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            str(binaries / "shellctl"),
            "serve",
            "--listen",
            f"127.0.0.1:{port}",
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with ShellctlClient(f"http://127.0.0.1:{port}", token="") as client:
                with anyio.fail_after(10):
                    while True:
                        assert process.returncode is None
                        try:
                            await client.health()
                            break
                        except httpx2.TransportError:
                            await anyio.sleep(0.05)
                yield client, tmp_path
        finally:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
            await anyio.run_process(
                ["tmux", "-S", str(tmux_socket), "kill-server"],
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
    reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with built shellctl binaries and tmux",
)
async def test_real_shellctl_postgres_and_application_capability(
    database, shell_server, seed_session, session_model
):
    client, remote_dir = shell_server
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
                PluginSpec(
                    plugin_provider="builtin",
                    plugin_name="shell",
                    config={
                        "base_url": client.base_url,
                        "cwd": str(remote_dir),
                        "token_env": "NO_TEST_TOKEN",
                    },
                )
            ],
        )
    )
    assert (await plugins.list_bindings(session.id))[0].state is None
    observations = []

    def run_model(messages, info):
        assert {tool.name for tool in info.function_tools} == {
            "builtin_shell_run",
            "builtin_shell_wait",
            "builtin_shell_input",
            "builtin_shell_interrupt",
        }
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "builtin_shell_run",
                        {
                            "script": (
                                "printf 'ready\\n'; read -r answer; "
                                "printf 'echo:%s\\n' \"$answer\"; sleep 30"
                            ),
                            "timeout": 0.2,
                        },
                    )
                ]
            )
        observations.append(str(returns[-1].content))
        return ModelResponse(parts=[TextPart("started")])

    session_model(FunctionModel(run_model))
    await service.enqueue_input(session.id, "queued", "start")
    assert (await service.start_runner(session.id)).output == "started"
    job_id = metadata(observations[0])["job_id"]
    assert not (await client.status(job_id)).done, await anyio.Path(
        remote_dir / "server.log"
    ).read_text()
    record = (await plugins.list_bindings(session.id))[0]
    assert job_id in ShellPluginState.model_validate(record.state).jobs
    # A fresh plugin/context reuses the persisted cursor and the real PTY.
    async with plugins.open_execution(session.id) as bindings:
        binding = bindings[0][2]
        tools = {tool.name: tool.function for tool in binding.tools}
        assert "echo:resume" in await tools["input"](job_id, "resume\n", timeout=2)
        assert "Unknown job" in await tools["wait"]("another-session")
        assert metadata(await tools["interrupt"](job_id, grace_seconds=0))["done"]
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert (await plugins.list_bindings(session.id))[0].state == {"jobs": {}}
    with pytest.raises(ShellctlClientError) as missing:
        await client.status(job_id)
    assert missing.value.code == "job_not_found"
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
