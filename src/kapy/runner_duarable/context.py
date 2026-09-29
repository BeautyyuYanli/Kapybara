"""Inject Worker-owned resources when the SDK reconstructs an Activity context."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic_ai.durable_exec.temporal import TemporalRunContext
from pydantic_ai.exceptions import UserError

from kapy.agent_output import AgentOutputService

from .types import RunnerDeps

_output_service: ContextVar[AgentOutputService] = ContextVar("runner_output_service")


@contextmanager
def bind_output_service(service: AgentOutputService) -> Iterator[None]:
    """Bind before starting Worker tasks, and reset after all Activities have ended."""
    with _output_service.set(service):
        yield


class RunnerActivityContext(TemporalRunContext[RunnerDeps]):
    """Borrow output on construction, including SDK dataclasses.replace() copies.

    The SDK codec excludes this resource reference; construction performs no I/O.
    Workflow code must never read the binding or instantiate this Activity context.
    """

    output_service: AgentOutputService

    def __init__(self, deps: RunnerDeps, **kwargs: Any) -> None:
        super().__init__(deps, **kwargs)
        try:
            self.output_service = _output_service.get()
        except LookupError as exc:
            raise UserError("Worker output service is not bound") from exc
