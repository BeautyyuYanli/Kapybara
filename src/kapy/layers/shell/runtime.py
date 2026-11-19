"""Run-local shellctl handles and calls backed by the layer's job inventory.

A lost creation response can leave a remote job without a local inventory entry.
Repeating that request could create another job.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio
import httpx2
from anyio.lowlevel import checkpoint_if_cancelled
from shellctl import JobMode, JobResult, ShellctlClient, ShellctlClientError
from shellctl.shared.constants import DEFAULT_TERMINATE_GRACE_SECONDS, DEFAULT_TIMEOUT_SECONDS

from .models import Config, NonEmpty, RunTimeout, WaitTimeout
from .output import Redactor, format_interrupt, format_output

REMOTE_ERRORS = (ShellctlClientError, httpx2.HTTPError)


@dataclass(frozen=True)
class ShellResource:
    client: ShellctlClient
    redact: Redactor


async def enter_conversation(conversation_id: str, config: Config) -> ShellResource:
    """Resolve credentials and construct a local client for the configured service."""
    token = os.environ.get(config.token_env, "")
    redact = Redactor(config.redact_patterns, token)
    return ShellResource(ShellctlClient(str(config.base_url), token=token), redact)


async def exit_conversation(resource: ShellResource) -> None:
    """Join the local HTTP close before propagating caller cancellation."""
    # Give the close task a native deadline that also reaches the HTTP pool's shield.
    close_task = asyncio.create_task(asyncio.wait_for(resource.client.close(), timeout=5))
    cancelled: asyncio.CancelledError | None = None
    with anyio.CancelScope(shield=True):
        while not close_task.done():
            try:
                # Waiting for completion keeps native caller cancellation local to this task.
                await asyncio.wait((close_task,))
            except asyncio.CancelledError as exc:
                cancelled = exc
    try:
        close_task.result()
    finally:
        if cancelled is not None:
            raise cancelled
        await checkpoint_if_cancelled()


def instructions(config: Config, prefix: str, redact: Redactor) -> str:
    return redact(
        f"Use {prefix}_run(script) to run a shell script in remote directory "
        f"{config.cwd!r}. A shebang may choose its interpreter. "
        f"Use {prefix}_wait(job_id) for more output, {prefix}_input(job_id, text) "
        f"for PTY input (include a newline to submit a line), and "
        f"{prefix}_interrupt(job_id) to terminate a job. Only this session's "
        "registered jobs are accepted. Timeout limits waiting for output, not "
        "job lifetime: done=false means the job may still be running. Tasks "
        "survive an execution. Shellctl manages job lifetime and garbage collection "
        "of terminal jobs and logs. Long output shows only head/tail "
        "and a remote full-log path. For ordered interactions on one job, wait "
        "for each tool result before sending the next call. A failed run may "
        "already have created a job; repeating run is not idempotent."
    )


class ShellOperations:
    """Remote operations with caller-supplied inventory callbacks.

    Layer callbacks explicitly replace State JSON.
    Register the ID before any tail read to update the caller's inventory; the
    caller owns persistence. A failed Activity retains its last saved State.
    """

    def __init__(
        self,
        config: Config,
        client: ShellctlClient,
        redact: Redactor,
        *,
        known: Callable[[str], Awaitable[int | None]],
        register: Callable[[str], Awaitable[None]],
        change: Callable[[str, int | None], Awaitable[None]],
    ) -> None:
        self.config, self.client, self.redact = config, client, redact
        self.known, self.register, self.change = known, register, change

    async def _remote_error(self, error: Exception, job_id: str) -> str:
        if isinstance(error, ShellctlClientError) and error.code == "job_not_found":
            await self.change(job_id, None)
        return self.redact.error(error, job_id)

    async def _result(self, result: JobResult) -> str:
        tail = None
        if result.truncated:
            try:
                tail = await self.client.tail(result.job_id)
            except REMOTE_ERRORS:
                pass  # Keep the original successful page and its cursor.
        await self.change(result.job_id, max(result.offset, tail.offset if tail else 0))
        return format_output(result, tail, self.redact)

    async def run(
        self,
        script: NonEmpty,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Create one PTY job; a transport error can follow a successful creation."""
        try:
            result = await self.client.run(
                script, cwd=self.config.cwd, env=self.config.env, mode=JobMode.PTY, timeout=timeout
            )
        except REMOTE_ERRORS as error:
            return self.redact.error(f"{error}; the job may already have been created")
        await self.register(result.job_id)
        return await self._result(result)

    async def wait(
        self,
        job_id: NonEmpty,
        timeout: WaitTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Read from the saved byte cursor; zero requests an immediate page."""
        offset = await self.known(job_id)
        if offset is None:
            return self.redact.error("Unknown job ID for this session", job_id)
        try:
            result = await self.client.wait(job_id, offset=offset, timeout=timeout)
        except REMOTE_ERRORS as error:
            return await self._remote_error(error, job_id)
        return await self._result(result)

    async def input(
        self,
        job_id: NonEmpty,
        text: str,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Send input once; a transport error does not undo that input."""
        offset = await self.known(job_id)
        if offset is None:
            return self.redact.error("Unknown job ID for this session", job_id)
        try:
            result = await self.client.input(job_id, text, offset=offset, timeout=timeout)
        except REMOTE_ERRORS as error:
            return await self._remote_error(error, job_id)
        return await self._result(result)

    async def interrupt(
        self, job_id: NonEmpty, grace_seconds: WaitTimeout = DEFAULT_TERMINATE_GRACE_SECONDS
    ) -> str:
        """Terminate without forgetting the job; retained logs remain readable."""
        if await self.known(job_id) is None:
            return self.redact.error("Unknown job ID for this session", job_id)
        try:
            result = await self.client.terminate(job_id, grace_seconds=grace_seconds)
        except REMOTE_ERRORS as error:
            return await self._remote_error(error, job_id)
        output_path = None
        try:
            output_path = (await self.client.tail(job_id)).output_path
        except REMOTE_ERRORS:
            pass  # Supplemental metadata must not undo an acknowledged interrupt.
        return format_interrupt(result, output_path, self.redact)
