import json

import httpx
import pytest

from shell_session_manager.shellctl.client import ShellctlClient, ShellctlClientError


@pytest.mark.anyio
async def test_shellctl_client_run_injects_headers_and_instance_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content.decode("utf-8"))
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
        await client.run(
            "printf ready\\n",
            cwd="/tmp",
            env={"HELLO": "world"},
            timeout=12,
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/jobs/run"
    assert captured["headers"]["authorization"] == "Bearer from-env"
    assert captured["json"] == {
        "script": "printf ready\\n",
        "cwd": "/tmp",
        "env": {"HELLO": "world"},
        "timeout": 12.0,
        "output_limit": 4096,
        "idle_flush_seconds": 0.25,
    }


@pytest.mark.anyio
async def test_shellctl_client_tail_uses_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/jobs/job-1/log/tail"
        assert request.url.params["output_limit"] == "1234"
        return httpx.Response(
            200,
            json={
                "job_id": "job-1",
                "done": False,
                "status": "running",
                "exit_code": None,
                "output_path": "/tmp/output.log",
                "output": "tail",
                "offset": 99,
                "truncated": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with ShellctlClient(
        "http://127.0.0.1:8765",
        output_limit=1234,
        token="secret",
        transport=transport,
    ) as client:
        result = await client.tail("job-1")

    assert result.output == "tail"
    assert result.offset == 99


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
