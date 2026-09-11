"""Runner values and same-database input callbacks; no transport or session CRUD."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic_ai.messages import ModelMessage, UserContent
from sqlalchemy.ext.asyncio import AsyncSession

type UserInput = str | Sequence[UserContent]
type NextStep = Literal["model_request", "handle_response", "done"]
type ConsumeInputs = Callable[[AsyncSession], Awaitable[None]]
type ConsumeCancel = Callable[[AsyncSession], Awaitable[bool]]


class SessionBusy(RuntimeError):
    """Another runner still owns a live execution lease."""


class RunnerLost(RuntimeError):
    """This runner's execution token has been replaced or removed."""


@dataclass(frozen=True, slots=True)
class InputBatch:
    """Nonempty read snapshot; consume borrows the runner's fenced transaction.

    The callback must delete only this snapshot's rows using the provided session.
    It must not commit, open another transaction, or access an external queue.
    """

    inputs: tuple[UserInput, ...]
    consume: ConsumeInputs

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError("InputBatch must contain at least one input")


type ReadInputs = Callable[[], Awaitable[InputBatch | None]]


@dataclass(frozen=True, slots=True)
class TurnResult[OutputT]:
    """finished reflects the current done state, not whether input ever existed.

    output contains a final result produced during this call, if any. A run keeps
    its just-produced output when the following boundary has no input or consumes
    cancel while still done. An empty turn, or reopening an already-done session
    without new input, returns output=None; historical output is not reconstructed.
    """

    finished: bool
    output: OutputT | None = None


@dataclass(frozen=True, slots=True)
class ResumeState:
    next_step: NextStep
    history: tuple[ModelMessage, ...]
