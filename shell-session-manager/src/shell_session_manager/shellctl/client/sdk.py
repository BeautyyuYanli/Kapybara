"""Public async SDK façade for shellctl transports.

`ShellctlClient` keeps the stable high-level workflow API while delegating wire
details to transport implementations. The primary constructor now selects the
transport from the endpoint URL scheme: `http://` / `https://` use the HTTP
transport, and `grpc://` / `grpcs://` use the grpclib transport.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx
from grpclib.client import Channel

from shell_session_manager.shellctl.client.common import (
    ShellctlClientDefaults,
    ShellctlClientError,
    ShellctlTransportProtocol,
    resolve_auth_token,
)
from shell_session_manager.shellctl.client.grpc import GrpcShellctlTransport
from shell_session_manager.shellctl.client.http import HttpShellctlTransport
from shell_session_manager.shellctl.shared import (
    DEFAULT_IDLE_FLUSH_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_OUTPUT_LIMIT_BYTES,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DeleteJobResponse,
    InputJobRequest,
    JobInfo,
    JobResult,
    JobStatusView,
    RunJobRequest,
    TerminalSize,
    TerminateJobRequest,
    WaitJobRequest,
)


class ShellctlClient:
    """Thin async SDK façade for the shellctl HTTP and gRPC APIs.

    The client keeps request defaults (`output_limit`, `idle_flush_seconds`, and
    bearer token handling) on the façade so HTTP and gRPC calls behave the same
    from the caller's perspective.
    """

    def __init__(
        self,
        base_url: str,
        *,
        output_limit: int = DEFAULT_OUTPUT_LIMIT_BYTES,
        idle_flush_seconds: float = DEFAULT_IDLE_FLUSH_SECONDS,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        ssl: object | None = None,
        _transport_impl: ShellctlTransportProtocol | None = None,
        _defaults: ShellctlClientDefaults | None = None,
        _channel: Channel | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        defaults = _defaults or ShellctlClientDefaults(
            output_limit=output_limit,
            idle_flush_seconds=idle_flush_seconds,
            token=resolve_auth_token(token),
        )
        self.output_limit = defaults.output_limit
        self.idle_flush_seconds = defaults.idle_flush_seconds
        self.token = defaults.token
        self._transport = _transport_impl or self._build_transport(
            self.base_url,
            defaults=defaults,
            client=client,
            transport=transport,
            ssl=ssl,
            channel=_channel,
        )

    async def __aenter__(self) -> ShellctlClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying transport resources if this SDK owns them."""

        await self._transport.close()

    async def healthz(self) -> dict[str, Any]:
        """Call the public health endpoint without requiring auth."""

        response = await self._transport.healthz()
        return response.model_dump(mode="json")

    async def run(
        self,
        script: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        terminal: TerminalSize | None = None,
    ) -> JobResult:
        """Create a new job and wait for initial output or completion.

        `cwd` and `env` preset the script's working directory and environment
        overlay on the server side.
        """

        request = RunJobRequest(
            script=script,
            cwd=cwd,
            env=env,
            terminal=terminal,
            timeout=timeout,
            output_limit=self.output_limit,
            idle_flush_seconds=self.idle_flush_seconds,
        )
        return await self._transport.run(request)

    async def wait(
        self,
        job_id: str,
        *,
        offset: int,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> JobResult:
        """Wait for incremental output, completion, truncation, or timeout."""

        return await self._transport.wait(
            job_id,
            WaitJobRequest(
                offset=offset,
                timeout=timeout,
                output_limit=self.output_limit,
                idle_flush_seconds=self.idle_flush_seconds,
            ),
        )

    async def status(self, job_id: str) -> JobStatusView:
        """Fetch the materialized status view for one job."""

        return await self._transport.status(job_id)

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[JobInfo]:
        """List recent jobs, optionally filtered by lifecycle status."""

        payload = await self._transport.list_jobs(status=status, limit=limit)
        return payload.jobs

    async def input(
        self,
        job_id: str,
        text: str,
        *,
        offset: int,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> JobResult:
        """Send text input to a running job and then wait like `wait()`."""

        return await self._transport.input(
            job_id,
            InputJobRequest(
                text=text,
                offset=offset,
                timeout=timeout,
                output_limit=self.output_limit,
                idle_flush_seconds=self.idle_flush_seconds,
            ),
        )

    async def tail(self, job_id: str) -> JobResult:
        """Fetch an immediate UTF-8-safe tail snapshot for a job."""

        return await self._transport.tail(job_id, output_limit=self.output_limit)

    async def terminate(
        self,
        job_id: str,
        grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> JobStatusView:
        """Terminate a job, returning the resulting materialized status view."""

        return await self._transport.terminate(
            job_id,
            TerminateJobRequest(grace_seconds=grace_seconds),
        )

    async def delete(
        self,
        job_id: str,
        *,
        force: bool = False,
        grace_seconds: float | None = None,
    ) -> DeleteJobResponse:
        """Delete job artifacts, optionally terminating the job first."""

        return await self._transport.delete(
            job_id,
            force=force,
            grace_seconds=grace_seconds,
        )

    @staticmethod
    def _build_transport(
        base_url: str,
        *,
        defaults: ShellctlClientDefaults,
        client: httpx.AsyncClient | None,
        transport: httpx.AsyncBaseTransport | None,
        ssl: object | None,
        channel: Channel | None,
    ) -> ShellctlTransportProtocol:
        """Create the transport selected by the endpoint URL scheme.

        HTTP endpoint URLs preserve the existing `httpx` injection hooks.
        gRPC endpoints intentionally use only `grpc://host:port` or
        `grpcs://host:port`; callers should not attach HTTP-only test transports
        to those schemes.
        """

        endpoint = urlsplit(base_url)
        scheme = endpoint.scheme.lower()

        if scheme in {"http", "https"}:
            if channel is not None:
                raise ValueError(
                    "channel is only supported for grpc:// or grpcs:// endpoints"
                )
            if ssl is not None:
                raise ValueError(
                    "ssl is only supported for grpc:// or grpcs:// endpoints"
                )
            return HttpShellctlTransport(
                base_url,
                defaults=defaults,
                client=client,
                transport=transport,
            )

        if scheme in {"grpc", "grpcs"}:
            if client is not None or transport is not None:
                raise ValueError(
                    "client and transport are only supported for http:// or https:// endpoints"
                )
            if endpoint.hostname is None or endpoint.port is None:
                raise ValueError(
                    "gRPC endpoints must use grpc://host:port or grpcs://host:port"
                )
            if endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment:
                raise ValueError(
                    "gRPC endpoints do not support path, query, or fragment components"
                )
            if scheme == "grpc" and ssl not in (None, False):
                raise ValueError("use grpcs:// to enable TLS for gRPC endpoints")
            if scheme == "grpcs" and ssl is False:
                raise ValueError(
                    "grpcs:// endpoints require TLS; omit ssl or pass TLS settings"
                )
            return GrpcShellctlTransport(
                host=endpoint.hostname,
                port=endpoint.port,
                defaults=defaults,
                ssl=True if scheme == "grpcs" and ssl is None else ssl,
                channel=channel,
            )

        if "://" not in base_url:
            raise ValueError(
                "shellctl endpoint must include a scheme such as http:// or grpc://"
            )
        raise ValueError(
            f"unsupported shellctl endpoint scheme: {scheme!r}; use http://, https://, grpc://, or grpcs://"
        )


__all__ = ["ShellctlClient", "ShellctlClientError"]
