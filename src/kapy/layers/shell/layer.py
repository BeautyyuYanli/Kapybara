"""Native shell layer; only explicit JSON writes retain job IDs and byte cursors.

Config validation and Toolset construction are pure. Each tool call owns one
HTTP client. Remote tasks survive runs; exit_scope releases the known jobs while
SessionService saves cleanup progress. State owns neither the shellctl service
nor the remote cwd/artifacts.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol
from uuid import UUID

import anyio
import httpx2
from pydantic_ai import RunContext, Tool
from pydantic_ai.messages import InstructionPart
from pydantic_ai.toolsets import FunctionToolset
from shellctl import ShellctlClientError
from shellctl.shared.constants import DEFAULT_TERMINATE_GRACE_SECONDS, DEFAULT_TIMEOUT_SECONDS

from kapy.layers.scopes import RetryableScopeError
from kapy.layers.types import HasLayers

from . import runtime
from .models import Config, NonEmpty, RunTimeout, State, WaitTimeout
from .output import Redactor


class ShellDeps(HasLayers, Protocol):
    session_id: UUID


class Toolset(FunctionToolset[ShellDeps]):
    """Sequential native tools sharing only the current deps.layers[name] JSON."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            id=name,
            tools=[
                Tool(self.run, name="builtin_shell_run", sequential=True),
                Tool(self.wait, name="builtin_shell_wait", sequential=True),
                Tool(self.input, name="builtin_shell_input", sequential=True),
                Tool(self.interrupt, name="builtin_shell_interrupt", sequential=True),
            ],
        )

    async def get_instructions(self, ctx: RunContext[ShellDeps]) -> list[InstructionPart]:
        config = Config.model_validate(ctx.deps.layers[self.name]["config"])
        return [
            InstructionPart(
                runtime.instructions(config, "builtin_shell", Redactor(config.redact_patterns, ""))
            )
        ]

    @asynccontextmanager
    async def _operations(
        self, ctx: RunContext[ShellDeps]
    ) -> AsyncIterator[runtime.ShellOperations]:
        entry = ctx.deps.layers[self.name]
        config = Config.model_validate(entry["config"])
        state = State.model_validate(entry["state"])

        async def known(job_id: str) -> int | None:
            return state.jobs.get(job_id)

        async def register(job_id: str) -> None:
            state.jobs.setdefault(job_id, 0)
            entry["state"] = state.model_dump(mode="json")

        async def change(job_id: str, offset: int | None) -> None:
            if offset is None:
                state.jobs.pop(job_id, None)
            elif job_id in state.jobs:
                state.jobs[job_id] = max(state.jobs[job_id], offset)
            entry["state"] = state.model_dump(mode="json")

        async with runtime.open_client(config) as (client, redact):
            yield runtime.ShellOperations(
                config,
                client,
                redact,
                session_id=ctx.deps.session_id,
                provider="builtin",
                name="shell",
                known=known,
                register=register,
                change=change,
            )

    async def run(
        self,
        ctx: RunContext[ShellDeps],
        script: NonEmpty,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Run a shell script in a new PTY job."""
        async with self._operations(ctx) as tools:
            return await tools.run(script, timeout)

    async def wait(
        self,
        ctx: RunContext[ShellDeps],
        job_id: NonEmpty,
        timeout: WaitTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Read more output from a session job."""
        async with self._operations(ctx) as tools:
            return await tools.wait(job_id, timeout)

    async def input(
        self,
        ctx: RunContext[ShellDeps],
        job_id: NonEmpty,
        text: str,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Send PTY input and read resulting output."""
        async with self._operations(ctx) as tools:
            return await tools.input(job_id, text, timeout)

    async def interrupt(
        self,
        ctx: RunContext[ShellDeps],
        job_id: NonEmpty,
        grace_seconds: WaitTimeout = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> str:
        """Interrupt a session job, retaining its log."""
        async with self._operations(ctx) as tools:
            return await tools.interrupt(job_id, grace_seconds)


async def enter_scope(scope_id: UUID, config: Config) -> State:
    """Start an empty job inventory; entering allocates no remote resources."""
    return State()


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


__all__ = ["Config", "State", "Toolset", "enter_scope", "exit_scope"]
