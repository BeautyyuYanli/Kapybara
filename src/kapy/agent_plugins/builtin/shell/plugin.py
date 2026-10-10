"""Session-owned shellctl jobs over a borrowed server, directory and HTTP client.

The only durable inventory is StateStore.jobs. Execution exit releases its client
but retains registered jobs; close deletes known jobs sequentially. HTTP effects
are never retried by CAS handling. A lost run response can leave an unknown job:
server-side ownership metadata supports reconciliation, not atomic allocation.
Neither known-ID checks nor shellctl itself provide session security isolation.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from shellctl import ShellctlClient

from kapy.agent_plugins.contracts import (
    AgentPlugin,
    PluginBinding,
    PluginTool,
    SessionContext,
    StateConflict,
    StateStore,
)
from kapy.layers.shell import runtime
from kapy.lifecycle import LifecycleError

from .models import ShellPluginConfig, ShellPluginState
from .output import Redactor

type ShellContext = SessionContext[ShellPluginConfig, ShellPluginState]
type ShellStore = StateStore[ShellPluginState]


@asynccontextmanager
async def _client(config: ShellPluginConfig) -> AsyncIterator[tuple[ShellctlClient, Redactor]]:
    async with runtime.open_client(config, client_type=ShellctlClient) as opened:
        yield opened


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
                    await runtime.delete_job(client, job_id)
                else:
                    # Read-back preserves any committed registration; if even the
                    # read fails, the outer handler records uncertainty, not delete.
                    current = await store.read()
                    if current.value is None or job_id not in current.value.jobs:
                        await runtime.delete_job(client, job_id)
        except BaseException as cleanup:
            error.add_note(f"shellctl job {job_id}: cleanup/read-back failed: {cleanup!r}")
        raise


class _ShellTools(runtime.ShellOperations):
    """Legacy inventory callbacks preserve per-job CAS and compensation."""

    def __init__(self, ctx: ShellContext, client: ShellctlClient, redact: Redactor) -> None:
        async def known(job_id: str) -> int | None:
            state = (await ctx.state.read()).value
            return None if state is None else state.jobs.get(job_id)

        async def register(job_id: str) -> None:
            await _register(ctx.state, client, job_id)

        async def change(job_id: str, offset: int | None) -> None:
            await _change_job(ctx.state, job_id, offset)

        super().__init__(
            ctx.config,
            client,
            redact,
            session_id=ctx.session_id,
            provider=ctx.plugin_provider,
            name=ctx.plugin_name,
            known=known,
            register=register,
            change=change,
        )


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
                    await runtime.delete_job(client, job_id)
                    del state.jobs[job_id]
                    previous = await ctx.state.replace(state, expected_revision=previous.revision)
