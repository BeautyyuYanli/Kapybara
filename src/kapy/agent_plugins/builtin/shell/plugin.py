"""Session-owned shellctl jobs over a borrowed server, directory and HTTP client.

The only durable inventory is StateStore.jobs. Execution exit releases its client
but retains registered jobs; close deletes known jobs sequentially. HTTP effects
are never retried by CAS handling. A lost run response can leave an unknown job:
server-side ownership metadata supports reconciliation, not atomic allocation.
Neither known-ID checks nor shellctl itself provide session security isolation.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2
from shellctl import JobMode, JobResult, ShellctlClient, ShellctlClientError
from shellctl.shared.constants import DEFAULT_TERMINATE_GRACE_SECONDS, DEFAULT_TIMEOUT_SECONDS

from kapy.agent_plugins.contracts import (
    AgentPlugin,
    PluginBinding,
    PluginTool,
    SessionContext,
    StateConflict,
    StateStore,
)
from kapy.lifecycle import LifecycleError

from .models import NonEmpty, RunTimeout, ShellPluginConfig, ShellPluginState, WaitTimeout
from .output import Redactor, format_interrupt, format_output

type ShellContext = SessionContext[ShellPluginConfig, ShellPluginState]
type ShellStore = StateStore[ShellPluginState]
REMOTE_ERRORS = (ShellctlClientError, httpx2.HTTPError)


@asynccontextmanager
async def _client(config: ShellPluginConfig) -> AsyncIterator[tuple[ShellctlClient, Redactor]]:
    token = os.environ.get(config.token_env, "")
    client = ShellctlClient(str(config.base_url), token=token)
    try:
        yield client, Redactor(config.redact_patterns, token)
    finally:
        # Cancellation must not leave a connection pool detached from its scope.
        with anyio.fail_after(5, shield=True):
            await client.close()


async def _change_job(store: ShellStore, job_id: str, offset: int | None) -> None:
    """Merge only this job; offset=None deletes, and progress never resurrects IDs."""
    while True:
        previous = await store.read()
        state = previous.value or ShellPluginState()
        if offset is None:
            if job_id not in state.jobs:
                return
            del state.jobs[job_id]
        elif job_id in state.jobs:
            if offset <= state.jobs[job_id]:
                return
            state.jobs[job_id] = offset
        else:
            return
        try:
            await store.replace(state, expected_revision=previous.revision)
            return
        except StateConflict:
            continue


async def _delete(client: ShellctlClient, job_id: str) -> None:
    try:
        result = await client.delete(job_id, force=True, grace_seconds=0)
        if not result.deleted:
            raise RuntimeError(f"shellctl did not confirm deletion of job {job_id}")
    except ShellctlClientError as error:
        if error.code != "job_not_found":
            raise


async def _register(store: ShellStore, client: ShellctlClient, job_id: str) -> None:
    """Register immediately; compensate only when absence/rejection is definite.

    A failed replace can have committed remotely. Read back under a bounded
    shield before deciding whether to delete; inaccessible state is uncertainty,
    not evidence of absence. Preserve the original failure and job ID for repair.
    """
    submitted = False
    try:
        while True:
            previous = await store.read()
            state = previous.value or ShellPluginState()
            if job_id in state.jobs:
                return
            state.jobs[job_id] = 0
            await anyio.lowlevel.checkpoint_if_cancelled()
            submitted = True
            try:
                await store.replace(state, expected_revision=previous.revision)
                return
            except StateConflict:
                submitted = False
    except BaseException as error:
        error.add_note(f"shellctl job {job_id}: registration failed; no run retry was attempted")
        try:
            with anyio.fail_after(10, shield=True):
                if isinstance(error, LifecycleError) or not submitted:
                    await _delete(client, job_id)
                else:
                    # Read-back preserves any committed registration; if even the
                    # read fails, the outer handler records uncertainty, not delete.
                    current = await store.read()
                    if current.value is None or job_id not in current.value.jobs:
                        await _delete(client, job_id)
        except BaseException as cleanup:
            error.add_note(f"shellctl job {job_id}: cleanup/read-back failed: {cleanup!r}")
        raise


class _ShellTools:
    """Callables confined to one execution; parallel calls merge only their job."""

    def __init__(self, ctx: ShellContext, client: ShellctlClient, redact: Redactor) -> None:
        self.ctx, self.client, self.redact = ctx, client, redact

    async def instructions(self) -> str:
        prefix = f"{self.ctx.plugin_provider}_{self.ctx.plugin_name}"
        return self.redact(
            f"Use {prefix}_run(script) to run a shell script in remote directory "
            f"{self.ctx.config.cwd!r}. A shebang may choose its interpreter. "
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

    async def _known(self, job_id: str) -> int | None:
        state = (await self.ctx.state.read()).value
        return None if state is None else state.jobs.get(job_id)

    async def _remote_error(self, error: Exception, job_id: str) -> str:
        if isinstance(error, ShellctlClientError) and error.code == "job_not_found":
            await _change_job(self.ctx.state, job_id, None)
        return self.redact.error(error, job_id)

    async def _result(self, result: JobResult) -> str:
        tail = None
        if result.truncated:
            try:
                tail = await self.client.tail(result.job_id)
            except REMOTE_ERRORS:
                pass  # The original successful page and cursor remain usable.
        await _change_job(
            self.ctx.state, result.job_id, max(result.offset, tail.offset if tail else 0)
        )
        return format_output(result, tail, self.redact)

    async def run(
        self,
        script: NonEmpty,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Create a PTY job; retain its cleanup reference before formatting output."""
        env = self.ctx.config.env | {
            "KAPY_SESSION_ID": str(self.ctx.session_id),
            "KAPY_PLUGIN_PROVIDER": self.ctx.plugin_provider,
            "KAPY_PLUGIN_NAME": self.ctx.plugin_name,
        }
        try:
            result = await self.client.run(
                script, cwd=self.ctx.config.cwd, env=env, mode=JobMode.PTY, timeout=timeout
            )
        except REMOTE_ERRORS as error:
            return self.redact.error(f"{error}; the job may already have been created")
        await _register(self.ctx.state, self.client, result.job_id)
        return await self._result(result)

    async def wait(
        self,
        job_id: NonEmpty,
        timeout: WaitTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Read from the saved cursor; zero timeout requests an immediate page."""
        offset = await self._known(job_id)
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
        """Send input once, then read output; a transport failure does not undo input."""
        offset = await self._known(job_id)
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
        """Terminate without forgetting the job; its output remains available."""
        if await self._known(job_id) is None:
            return self.redact.error("Unknown job ID for this session", job_id)
        try:
            result = await self.client.terminate(job_id, grace_seconds=grace_seconds)
        except REMOTE_ERRORS as error:
            return await self._remote_error(error, job_id)
        output_path = None
        try:
            output_path = (await self.client.tail(job_id)).output_path
        except REMOTE_ERRORS:
            pass  # Failure to supplement metadata must not undo a successful interrupt.
        return format_interrupt(result, output_path, self.redact)


class ShellPlugin(AgentPlugin[ShellPluginConfig, ShellPluginState]):
    """Fresh local clients per operation, persistent jobs until session close.

    Close uses shellctl deletion acknowledgement, not a stronger guarantee that
    detached descendants or every artifact are gone. Unknown run-response orphans
    require server-side reconciliation of .job-env.json ownership metadata; GC
    only expires terminal jobs. No service/cwd ownership or global list cleanup.
    """

    @asynccontextmanager
    async def open_execution(self, ctx: ShellContext) -> AsyncIterator[PluginBinding]:
        async with _client(ctx.config) as (client, redact):
            tools = _ShellTools(ctx, client, redact)
            yield PluginBinding(
                instructions=tools.instructions,
                tools=(
                    PluginTool("run", "Run a shell script in a new PTY job.", tools.run),
                    PluginTool("wait", "Read more output from a session job.", tools.wait),
                    PluginTool("input", "Send PTY input and read resulting output.", tools.input),
                    PluginTool(
                        "interrupt", "Interrupt a session job, retaining its log.", tools.interrupt
                    ),
                ),
            )

    async def close_session(self, ctx: ShellContext) -> None:
        """Sequential bounded deletion, preserving partial progress for a retry."""
        with anyio.fail_after(60):
            previous = await ctx.state.read()
            state = previous.value
            if state is None or not state.jobs:
                return
            async with _client(ctx.config) as (client, _):
                for job_id in sorted(state.jobs):
                    await _delete(client, job_id)
                    del state.jobs[job_id]
                    previous = await ctx.state.replace(state, expected_revision=previous.revision)
