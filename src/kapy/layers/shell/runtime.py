"""Shared shellctl calls; the caller owns job inventory and its write semantics.

Remote effects are never repeated to resolve a state conflict. A lost creation
response can leave an unknown job; ownership metadata supports reconciliation,
not allocation atomicity or session security isolation.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

import anyio
import httpx2
from shellctl import JobMode, JobResult, ShellctlClient, ShellctlClientError
from shellctl.shared.constants import DEFAULT_TERMINATE_GRACE_SECONDS, DEFAULT_TIMEOUT_SECONDS

from .models import Config, NonEmpty, RunTimeout, WaitTimeout
from .output import Redactor, format_interrupt, format_output

REMOTE_ERRORS = (ShellctlClientError, httpx2.HTTPError)


@asynccontextmanager
async def open_client(
    config: Config, *, client_type: type[ShellctlClient] = ShellctlClient
) -> AsyncIterator[tuple[ShellctlClient, Redactor]]:
    """Resolve the token only at an execution/close boundary and always close HTTP."""
    token = os.environ.get(config.token_env, "")
    client = client_type(str(config.base_url), token=token)
    try:
        yield client, Redactor(config.redact_patterns, token)
    finally:
        with anyio.fail_after(5, shield=True):
            await client.close()


async def delete_job(client: ShellctlClient, job_id: str) -> None:
    """Require acknowledged deletion, accepting an already absent job for retries."""
    try:
        result = await client.delete(job_id, force=True, grace_seconds=0)
        if not result.deleted:
            raise RuntimeError(f"shellctl did not confirm deletion of job {job_id}")
    except ShellctlClientError as error:
        if error.code != "job_not_found":
            raise


def instructions(config: Config, prefix: str, redact: Redactor) -> str:
    return redact(
        f"Use {prefix}_run(script) to run a shell script in remote directory "
        f"{config.cwd!r}. A shebang may choose its interpreter. "
        f"Use {prefix}_wait(job_id) for more output, {prefix}_input(job_id, text) "
        f"for PTY input (include a newline to submit a line), and "
        f"{prefix}_interrupt(job_id) to terminate a job. Only this session's "
        "registered jobs are accepted. Timeout limits waiting for output, not "
        "job lifetime: done=false means the job may still be running. Tasks "
        "survive an execution and are cleaned up at session close; shellctl "
        "may GC terminal jobs and logs sooner. Long output shows only head/tail "
        "and a remote full-log path. For ordered interactions on one job, wait "
        "for each tool result before sending the next call. A failed run may "
        "already have created a job; repeating run is not idempotent."
    )


class ShellOperations:
    """Remote operations with caller-supplied inventory callbacks.

    Layer callbacks explicitly replace State JSON.
    Register the ID before any tail read to update the caller's inventory; the
    caller owns persistence. Durable Activity failure/cancellation does not commit
    its local layer State.
    """

    def __init__(
        self,
        config: Config,
        client: ShellctlClient,
        redact: Redactor,
        *,
        session_id: UUID,
        provider: str,
        name: str,
        known: Callable[[str], Awaitable[int | None]],
        register: Callable[[str], Awaitable[None]],
        change: Callable[[str, int | None], Awaitable[None]],
    ) -> None:
        self.config, self.client, self.redact = config, client, redact
        self.session_id, self.provider, self.name = session_id, provider, name
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
        env = self.config.env | {
            "KAPY_SESSION_ID": str(self.session_id),
            "KAPY_PLUGIN_PROVIDER": self.provider,
            "KAPY_PLUGIN_NAME": self.name,
        }
        try:
            result = await self.client.run(
                script, cwd=self.config.cwd, env=env, mode=JobMode.PTY, timeout=timeout
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
