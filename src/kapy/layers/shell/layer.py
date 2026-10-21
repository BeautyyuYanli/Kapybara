"""Native shell layer; only explicit JSON writes retain job IDs and byte cursors.

Config validation and factory assembly are pure. Each tool call owns one
HTTP client. Remote tasks survive runs; exit_scope releases the known jobs while
SessionService saves cleanup progress. State owns neither the shellctl service
nor the remote cwd/artifacts. Deps are opaque: injected access and Config/State
supply all layer data, including the scope identity established by enter.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import anyio
import httpx2
from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.toolsets import FunctionToolset
from shellctl import ShellctlClientError
from shellctl.shared.constants import DEFAULT_TERMINATE_GRACE_SECONDS, DEFAULT_TIMEOUT_SECONDS

from pydantic_ai_layer_protocol import RetryableScopeError
from pydantic_ai_layer_protocol.registry import dump_object
from pydantic_ai_layer_protocol.types import Layer, LayerAccess

from . import runtime
from .models import Config, NonEmpty, RunTimeout, State, WaitTimeout
from .output import Redactor


def make[D](layer_id: str, access: LayerAccess[D]) -> Layer[D, Config, State]:
    """Build fixed native tools; each call reads and writes its current deps entry."""

    def read(deps: D) -> tuple[Config, State]:
        entry = access(deps)
        return Config.model_validate(entry["config"]), State.model_validate(entry["state"])

    def instructions(ctx: RunContext[D]) -> str:
        config = Config.model_validate(access(ctx.deps)["config"])
        return runtime.instructions(config, "builtin_shell", Redactor(config.redact_patterns, ""))

    @asynccontextmanager
    async def operations(ctx: RunContext[D]) -> AsyncIterator[runtime.ShellOperations]:
        config, state = read(ctx.deps)

        async def known(job_id: str) -> int | None:
            return state.jobs.get(job_id)

        async def register(job_id: str) -> None:
            state.jobs.setdefault(job_id, 0)
            access(ctx.deps)["state"] = dump_object(state)

        async def change(job_id: str, offset: int | None) -> None:
            if offset is None:
                state.jobs.pop(job_id, None)
            elif job_id in state.jobs:
                state.jobs[job_id] = max(state.jobs[job_id], offset)
            access(ctx.deps)["state"] = dump_object(state)

        async with runtime.open_client(config) as (client, redact):
            yield runtime.ShellOperations(
                config,
                client,
                redact,
                session_id=state.scope_id,
                provider="builtin",
                name="shell",
                known=known,
                register=register,
                change=change,
            )

    async def run(
        ctx: RunContext[D],
        script: NonEmpty,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Run a shell script in a new PTY job."""
        async with operations(ctx) as tools:
            return await tools.run(script, timeout)

    async def wait(
        ctx: RunContext[D],
        job_id: NonEmpty,
        timeout: WaitTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Read more output from a session job."""
        async with operations(ctx) as tools:
            return await tools.wait(job_id, timeout)

    async def input(
        ctx: RunContext[D],
        job_id: NonEmpty,
        text: str,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Send PTY input and read resulting output."""
        async with operations(ctx) as tools:
            return await tools.input(job_id, text, timeout)

    async def interrupt(
        ctx: RunContext[D],
        job_id: NonEmpty,
        grace_seconds: WaitTimeout = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> str:
        """Interrupt a session job, retaining its log."""
        async with operations(ctx) as tools:
            return await tools.interrupt(job_id, grace_seconds)

    return Layer(
        config_type=Config,
        state_type=State,
        capability=Capability(
            id=layer_id,
            toolsets=[
                FunctionToolset[D](
                    id=layer_id,
                    instructions=instructions,
                    tools=[
                        Tool(run, name="builtin_shell_run", sequential=True),
                        Tool(wait, name="builtin_shell_wait", sequential=True),
                        Tool(input, name="builtin_shell_input", sequential=True),
                        Tool(interrupt, name="builtin_shell_interrupt", sequential=True),
                    ],
                )
            ],
        ),
        enter_scope=enter_scope,
        exit_scope=exit_scope,
    )


async def enter_scope(scope_id: UUID, config: Config) -> State:
    """Record scope identity with an empty inventory, without allocating resources."""
    return State(scope_id=scope_id)


async def exit_scope(scope_id: UUID, config: Config, state: State | None) -> None:
    """Delete known jobs, removing only acknowledged or already absent IDs.

    The host supplies exclusive scope ownership and persists the mutated State
    after every attempt. Deletion is idempotent, including lost acknowledgements.
    Connection failures, timeouts, interrupted remote protocols and temporary
    HTTP availability errors opt into retry. Local configuration/protocol and
    authentication errors propagate unchanged.

    None denotes abandoned initialization. Enter allocates nothing, and tools
    cannot run before ready, so that case has no jobs to release. Ready cleanup
    must receive its saved inventory; shellctl cannot discover jobs by scope.
    State may not reflect the latest upstream state after a crash, an unsaved tool
    mutation or an unacknowledged job creation. Jobs missing from that inventory
    cannot be discovered here and may remain as orphaned upstream resources.
    """
    if state is None or not state.jobs:
        return
    try:
        with anyio.fail_after(60):
            async with runtime.open_client(config) as (client, _):
                for job_id in sorted(state.jobs):
                    await runtime.delete_job(client, job_id)
                    del state.jobs[job_id]
    except (
        httpx2.NetworkError,
        httpx2.TimeoutException,
        httpx2.RemoteProtocolError,
        TimeoutError,
    ) as error:
        raise RetryableScopeError("shellctl scope cleanup temporarily unavailable") from error
    except ShellctlClientError as error:
        if error.status_code in {429, 502, 503, 504}:
            raise RetryableScopeError("shellctl scope cleanup temporarily unavailable") from error
        raise
