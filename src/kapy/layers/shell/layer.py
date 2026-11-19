"""Native shell layer; only explicit JSON writes retain job IDs and byte cursors.

The host opens one local HTTP client for the run. Tools use the saved job
inventory to route calls and track output; shellctl owns remote job retention.
"""

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.toolsets import FunctionToolset
from shellctl.shared.constants import DEFAULT_TERMINATE_GRACE_SECONDS, DEFAULT_TIMEOUT_SECONDS

from pydantic_ai_layer_protocol import Layer, LayerAccess, dump_object

from . import runtime
from .models import Config, NonEmpty, RunTimeout, State, WaitTimeout
from .output import Redactor


def make[D](
    layer_id: str, access: LayerAccess[D]
) -> Layer[D, Config, State, runtime.ShellResource]:
    """Build fixed native tools; each call reads and writes its current deps entry."""

    def instructions(ctx: RunContext[D]) -> str:
        config = Config.model_validate(access.data(ctx.deps)["config"])
        return runtime.instructions(config, "builtin_shell", Redactor(config.redact_patterns, ""))

    def operations(ctx: RunContext[D]) -> runtime.ShellOperations:
        resource: runtime.ShellResource = access.resource(ctx.deps)
        entry = access.data(ctx.deps)
        config = Config.model_validate(entry["config"])
        state = State.model_validate(entry["state"])

        async def known(job_id: str) -> int | None:
            return state.jobs.get(job_id)

        async def register(job_id: str) -> None:
            state.jobs.setdefault(job_id, 0)
            entry["state"] = dump_object(state)

        async def change(job_id: str, offset: int | None) -> None:
            if offset is None:
                state.jobs.pop(job_id, None)
            elif job_id in state.jobs:
                state.jobs[job_id] = max(state.jobs[job_id], offset)
            entry["state"] = dump_object(state)

        return runtime.ShellOperations(
            config,
            resource.client,
            resource.redact,
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
        return await operations(ctx).run(script, timeout)

    async def wait(
        ctx: RunContext[D],
        job_id: NonEmpty,
        timeout: WaitTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Read more output from a session job."""
        return await operations(ctx).wait(job_id, timeout)

    async def input(
        ctx: RunContext[D],
        job_id: NonEmpty,
        text: str,
        timeout: RunTimeout = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - server wait budget
    ) -> str:
        """Send PTY input and read resulting output."""
        return await operations(ctx).input(job_id, text, timeout)

    async def interrupt(
        ctx: RunContext[D],
        job_id: NonEmpty,
        grace_seconds: WaitTimeout = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> str:
        """Interrupt a session job, retaining its log."""
        return await operations(ctx).interrupt(job_id, grace_seconds)

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
        enter_conversation=runtime.enter_conversation,
        exit_conversation=runtime.exit_conversation,
    )
