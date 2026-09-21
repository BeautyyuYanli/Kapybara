"""Context paging contracts and bounded history assembly, independent of summary policy.

A strategy owns the replaceable prefix. The execution core appends the original
checkpoint suffix, expanded backwards for tool pairs. Callbacks borrow no live
transaction; their reads open short transactions bounded by the supplied cursor.
"""

from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .repository import AgentRepository
from .types import ContextPage, NextStep

type JsonObject = dict[str, JsonValue]
type HistoryRows = list[tuple[int, ModelMessage]]


class ReadHistory(Protocol):
    async def __call__(
        self, *, start_seq: int = 0, through_seq: int | None = None
    ) -> HistoryRows: ...


class ReadHistoryBefore(Protocol):
    async def __call__(self, *, through_seq: int | None = None, limit: int = 64) -> HistoryRows: ...


@dataclass(frozen=True, slots=True)
class PageBoundary:
    next_step: NextStep
    last_seq: int
    previous_anchor_seq: int | None
    latest_response_seq: int | None
    response_tokens: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class PageTurnContext:
    session_id: UUID
    anchor_seq: int
    previous_page: ContextPage | None
    messages: list[ModelMessage]
    operation_id: str
    read_history: ReadHistory
    read_history_before: ReadHistoryBefore


@dataclass(frozen=True, slots=True)
class ContextAssemblyContext:
    session_id: UUID
    page: ContextPage | None
    prefix_through_seq: int
    read_history: ReadHistory
    read_history_before: ReadHistoryBefore


type PageTrigger = Callable[[PageBoundary], bool]
type PageTurnAction = Callable[[PageTurnContext], Awaitable[JsonObject]]
type ContextAssembler = Callable[[ContextAssemblyContext], Awaitable[list[ModelMessage]]]


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """One runner's strategy. key versions the durable payload interpretation."""

    key: str
    should_turn: PageTrigger
    on_turn: PageTurnAction | None
    assemble: ContextAssembler

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("Context policy key must be nonempty")


async def _full_history(context: ContextAssemblyContext) -> list[ModelMessage]:
    return [message for _, message in await context.read_history()]


def full_history_policy() -> ContextPolicy:
    """Retain all history, without automatic paging or an external action."""
    return ContextPolicy("history/v1", lambda boundary: False, None, _full_history)


def bounded_history(
    session_factory: async_sessionmaker[AsyncSession], session_id: UUID, upper: int
) -> tuple[ReadHistory, ReadHistoryBefore]:
    """Bind reads to one session and inclusive upper bound, including an empty prefix."""

    async def read_history(*, start_seq: int = 0, through_seq: int | None = None) -> HistoryRows:
        if type(start_seq) is not int or start_seq < 0:
            raise ValueError("start_seq must be a nonnegative integer")
        end = upper if through_seq is None else min(through_seq, upper)
        if end < start_seq:
            return []
        async with session_factory.begin() as db:
            return await AgentRepository(db).read_history(
                session_id, start_seq=start_seq, through_seq=end
            )

    async def read_before(*, through_seq: int | None = None, limit: int = 64) -> HistoryRows:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        end = upper if through_seq is None else min(through_seq, upper)
        if end < 0:
            return []
        async with session_factory.begin() as db:
            return await AgentRepository(db).read_history_before(
                session_id, through_seq=end, limit=limit
            )

    return read_history, read_before


def close_tool_pairs(rows: Sequence[tuple[int, ModelMessage]], start: int) -> int | None:
    """Expand an index backwards until every retained tool reply has its call.

    None requests an older page. Match replies only to earlier, still-open calls;
    a later reuse of the same ID cannot supply an older reply's missing call.
    Unanswered calls are valid only at the pending response checkpoint.
    """
    while True:
        open_calls: dict[tuple[str, str], int] = {}
        earliest = start
        for index, (_, message) in enumerate(rows):
            if isinstance(message, ModelResponse):
                for part in message.parts:
                    if isinstance(part, ToolCallPart):
                        open_calls[part.tool_name, part.tool_call_id] = index
            else:
                for part in message.parts:
                    if (
                        not isinstance(part, (ToolReturnPart, RetryPromptPart))
                        or part.tool_name is None
                    ):
                        continue
                    call = open_calls.pop((part.tool_name, part.tool_call_id), None)
                    if index < start:
                        continue
                    if call is None:
                        if rows[0][0] == 0:
                            raise RuntimeError("History contains a tool reply without its call")
                        return None
                    earliest = min(earliest, call)
        if earliest == start:
            return start
        start = earliest


async def assemble_working_context(
    *,
    session_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    next_step: NextStep,
    next_seq: int,
    page: ContextPage | None,
    policy: ContextPolicy,
) -> list[ModelMessage]:
    """Protect continuation and post-anchor history before invoking the assembler."""
    if page is not None and page.policy_key != policy.key:
        raise ValueError(f"Context page policy {page.policy_key!r} does not match {policy.key!r}")
    through = next_seq - 1
    read, before = bounded_history(session_factory, session_id, through)
    suffix: HistoryRows = []
    start = next_seq
    if next_step != "done" or (page is not None and page.anchor_seq < through):
        rows: HistoryRows = []
        cursor = through
        while True:
            chunk = await before(through_seq=cursor)
            if not chunk:
                raise RuntimeError("Checkpoint history is missing")
            rows[:0] = reversed(chunk)
            checkpoint = len(rows)
            if next_step != "done":
                checkpoint -= 1
                expected = ModelResponse if next_step == "handle_response" else ModelRequest
                if not isinstance(rows[-1][1], expected):
                    raise RuntimeError(f"{next_step} history has an invalid final message")
                while checkpoint > 0 and isinstance(rows[checkpoint - 1][1], ModelRequest):
                    checkpoint -= 1
                if checkpoint == 0 and rows[0][0] != 0:
                    cursor = rows[0][0] - 1
                    continue
            protected = rows[checkpoint][0] if checkpoint < len(rows) else next_seq
            if page is not None:
                protected = min(protected, page.anchor_seq + 1)
            if protected < rows[0][0]:
                cursor = rows[0][0] - 1
                continue
            index = next((i for i, (seq, _) in enumerate(rows) if seq >= protected), len(rows))
            closed = close_tool_pairs(rows, index)
            if closed is None:
                cursor = rows[0][0] - 1
                continue
            suffix = rows[closed:]
            start = suffix[0][0] if suffix else next_seq
            break
    prefix_read, prefix_before = bounded_history(session_factory, session_id, start - 1)
    prefix = await policy.assemble(
        ContextAssemblyContext(
            session_id,
            deepcopy(page),
            start - 1,
            prefix_read,
            prefix_before,
        )
    )
    return deepcopy([*prefix, *(message for _, message in suffix)])
