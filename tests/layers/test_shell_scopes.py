"""Shell scope cleanup uses the real SDK request/error and client-close contracts."""

import os
from functools import partial
from uuid import uuid4

import httpx2
import pytest
from shellctl import ShellctlClient, ShellctlClientError
from shellctl_fixture import shell_server as shell_server

from kapy.layers import RetryableScopeError
from kapy.layers.scopes import retry_scope
from kapy.layers.shell import layer, runtime

pytestmark = pytest.mark.asyncio


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
    assert state == layer.State() and not requests and not closed
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
    assert state.jobs == {"c": 4} and len(closed) == 1
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
    state = layer.State(jobs={"known": 5})
    expected = (
        RetryableScopeError
        if retryable
        else type(failure)
        if isinstance(failure, Exception)
        else ShellctlClientError
    )
    with pytest.raises(expected):
        await layer.exit_scope(uuid4(), layer.Config(cwd="/remote/work"), state)
    assert state.jobs == {"known": 5} and len(closed) == 1


@pytest.mark.skipif(
    "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
    reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with tmux and shellctl binaries",
)
async def test_invalid_token_protocol_error_from_real_sdk_does_not_retry(shell_server, monkeypatch):
    client, cwd = shell_server
    monkeypatch.setenv("SCOPE_INVALID_TOKEN", "invalid\nheader")
    config = layer.Config(cwd=str(cwd), base_url=client.base_url, token_env="SCOPE_INVALID_TOKEN")
    scope_id, state, calls = uuid4(), layer.State(jobs={"known": 0}), []

    async def exit_scope():
        calls.append(scope_id)
        await layer.exit_scope(scope_id, config, state)

    with pytest.raises(httpx2.LocalProtocolError):
        await retry_scope(exit_scope)
    assert calls == [scope_id] and state.jobs == {"known": 0}
