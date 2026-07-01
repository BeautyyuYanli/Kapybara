import json
from collections.abc import Awaitable, Callable
from typing import ClassVar

import httpx
import pytest

from shell_session_manager.shellctl.client import ShellctlClient, ShellctlClientError
from shell_session_manager.shellctl.client import sdk as shellctl_sdk


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("call", "expected_path", "expected_json", "wait_timeout"),
    [
        (
            lambda client: client.run(
                "printf ready\\n",
                cwd="/tmp",
                env={"HELLO": "world"},
                timeout=12,
            ),
            "/v1/jobs/run",
            {
                "script": "printf ready\\n",
                "cwd": "/tmp",
                "env": {"HELLO": "world"},
                "timeout": 12.0,
                "output_limit": 4096,
                "idle_flush_seconds": 0.25,
            },
            22.0,
        ),
        (
            lambda client: client.wait("job-1", offset=3, timeout=7),
            "/v1/jobs/job-1/wait",
            {
                "offset": 3,
                "timeout": 7,
                "output_limit": 4096,
                "idle_flush_seconds": 0.25,
            },
            17.0,
        ),
        (
            lambda client: client.input("job-1", "ls\\n", offset=5, timeout=9),
            "/v1/jobs/job-1/input",
            {
                "text": "ls\\n",
                "offset": 5,
                "timeout": 9,
                "output_limit": 4096,
                "idle_flush_seconds": 0.25,
            },
            19.0,
        ),
    ],
)
async def test_shellctl_client_blocking_calls_use_grace_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[ShellctlClient], Awaitable[object]],
    expected_path: str,
    expected_json: dict[str, object],
    wait_timeout: float,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content.decode("utf-8"))
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            json={
                "job_id": "05211530-k7p",
                "done": False,
                "status": "running",
                "exit_code": None,
                "output_path": "/tmp/output.log",
                "output": "",
                "offset": 0,
                "truncated": False,
            },
        )

    monkeypatch.setenv("SHELLCTL_AUTH_TOKEN", "from-env")
    transport = httpx.MockTransport(handler)
    async with ShellctlClient(
        "http://127.0.0.1:8765",
        output_limit=4096,
        idle_flush_seconds=0.25,
        transport=transport,
    ) as client:
        await call(client)

    assert captured["method"] == "POST"
    assert captured["path"] == expected_path
    assert captured["headers"]["authorization"] == "Bearer from-env"
    assert captured["json"] == expected_json
    assert captured["timeout"] == {
        "connect": 30.0,
        "read": wait_timeout,
        "write": 30.0,
        "pool": 30.0,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("call", "expected_method", "expected_path", "response_json", "assert_result"),
    [
        (
            lambda client: client.tail("job-1"),
            "GET",
            "/v1/jobs/job-1/log/tail",
            {
                "job_id": "job-1",
                "done": False,
                "status": "running",
                "exit_code": None,
                "output_path": "/tmp/output.log",
                "output": "tail",
                "offset": 99,
                "truncated": False,
            },
            lambda result: (result.output, result.offset) == ("tail", 99),
        ),
        (
            lambda client: client.status("job-1"),
            "GET",
            "/v1/jobs/job-1",
            {
                "job_id": "job-1",
                "status": "running",
                "done": False,
                "exit_code": None,
                "created_at": "2026-01-01T00:00:00Z",
                "started_at": "2026-01-01T00:00:01Z",
                "ended_at": None,
                "offset": 99,
            },
            lambda result: (result.status, result.offset) == ("running", 99),
        ),
        (
            lambda client: client.terminate("job-1", grace_seconds=7.0),
            "POST",
            "/v1/jobs/job-1/terminate",
            {
                "job_id": "job-1",
                "status": "running",
                "done": False,
                "exit_code": None,
                "created_at": "2026-01-01T00:00:00Z",
                "started_at": "2026-01-01T00:00:01Z",
                "ended_at": None,
                "offset": 99,
            },
            lambda result: (result.status, result.offset) == ("running", 99),
        ),
        (
            lambda client: client.delete("job-1", force=True, grace_seconds=3.5),
            "DELETE",
            "/v1/jobs/job-1",
            {"job_id": "job-1", "deleted": True},
            lambda result: (result.job_id, result.deleted) == ("job-1", True),
        ),
    ],
)
async def test_shellctl_client_control_calls_use_default_timeout(
    call: Callable[[ShellctlClient], Awaitable[object]],
    expected_method: str,
    expected_path: str,
    response_json: dict[str, object],
    assert_result: Callable[[object], bool],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == expected_method
        assert request.url.path == expected_path
        assert request.extensions["timeout"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }
        return httpx.Response(200, json=response_json)

    transport = httpx.MockTransport(handler)
    async with ShellctlClient(
        "http://127.0.0.1:8765",
        output_limit=1234,
        token="secret",
        transport=transport,
    ) as client:
        result = await call(client)

    assert assert_result(result)


@pytest.mark.anyio
async def test_shellctl_client_closes_owned_client_on_close_and_context_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    class TrackingAsyncClient(httpx.AsyncClient):
        created_clients: ClassVar[list["TrackingAsyncClient"]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            self.__class__.created_clients.append(self)

        async def aclose(self) -> None:
            self.close_calls += 1
            await super().aclose()

    monkeypatch.setattr(shellctl_sdk.httpx, "AsyncClient", TrackingAsyncClient)
    close_client = ShellctlClient(
        "http://127.0.0.1:8765",
        transport=httpx.MockTransport(handler),
    )
    direct_owned_client = TrackingAsyncClient.created_clients[-1]
    await close_client.close()
    assert direct_owned_client.close_calls == 1

    async with ShellctlClient(
        "http://127.0.0.1:8765",
        transport=httpx.MockTransport(handler),
    ) as context_client:
        context_owned_client = TrackingAsyncClient.created_clients[-1]
        await context_client.healthz()

    assert context_owned_client.close_calls == 1


@pytest.mark.anyio
async def test_shellctl_client_raises_structured_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "job_not_running", "message": "already done"}},
        )

    transport = httpx.MockTransport(handler)
    async with ShellctlClient(
        "http://127.0.0.1:8765", token="secret", transport=transport
    ) as client:
        with pytest.raises(ShellctlClientError, match="job_not_running"):
            await client.input("job-1", "ls\n", offset=0)


@pytest.mark.anyio
async def test_shellctl_client_does_not_close_injected_client() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    class TrackingAsyncClient(httpx.AsyncClient):
        def __init__(self) -> None:
            super().__init__(
                base_url="http://127.0.0.1:8765",
                transport=httpx.MockTransport(handler),
            )
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            await super().aclose()

    injected_client = TrackingAsyncClient()

    async with ShellctlClient(
        "http://127.0.0.1:8765",
        client=injected_client,
    ) as client:
        await client.healthz()

    assert injected_client.close_calls == 0
    await injected_client.aclose()
    assert injected_client.close_calls == 1
