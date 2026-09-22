"""Host-owned paging, raw windows and context plugin contracts.

Plugins process closed pages and supply upper context. The host owns anchors,
checkpoint/tool-pair protection and final assembly; all reads use short transactions.
"""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol, overload
from uuid import UUID

from pydantic import BaseModel, JsonValue
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .repository import AgentRepository
from .types import ContextPageRecord, NextStep

type JsonObject = dict[str, JsonValue]
type HistoryRows = list[tuple[int, ModelMessage]]


class CallAgent(Protocol):
    """Borrow a stable text-output Agent while on_page is active.

    Await calls sequentially in the runner's owning task. Child-task wrappers,
    including asyncio.wait_for(call_agent(...), timeout), fail the owner check.
    result_type validates returned text without changing the SDK output channel.
    """

    @overload
    async def __call__(
        self, prompt: str, *, result_type: None = None, block_other_tools: bool = False
    ) -> str: ...

    @overload
    async def __call__[T: BaseModel](
        self, prompt: str, *, result_type: type[T], block_other_tools: bool = False
    ) -> T: ...


@dataclass(frozen=True, slots=True)
class ContextPage:
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class PageInput:
    previous_page: ContextPage | None
    messages: Sequence[ModelMessage]


@dataclass(frozen=True, slots=True)
class ContextInput:
    page: ContextPage
    messages: Sequence[ModelMessage]


class ContextPlugin(Protocol):
    key: str

    async def on_page(self, page: PageInput, *, call_agent: CallAgent) -> ContextPage: ...

    async def get_context(self, context: ContextInput) -> list[ModelMessage]: ...


class ReadHistory(Protocol):
    async def __call__(
        self, *, start_seq: int = 0, through_seq: int | None = None
    ) -> HistoryRows: ...


class ReadHistoryBefore(Protocol):
    async def __call__(self, *, through_seq: int | None = None, limit: int = 64) -> HistoryRows: ...


def require_nonnegative_int(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


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


def replay_start(rows: Sequence[tuple[int, ModelMessage]], turns: int) -> int | None:
    """Find an absolute start in an ascending, contiguous suffix ending at the anchor.

    None requests an older page. The caller passes turns > 0. Include the request
    segment preceding the Nth response, then extend backwards until every local tool
    reply has its call. A page's extra prefix never becomes part of the window merely
    because it was fetched. Invalid history with an orphaned reply raises RuntimeError.
    """
    responses = [
        index for index, (_, message) in enumerate(rows) if isinstance(message, ModelResponse)
    ]
    if len(responses) < turns:
        return 0 if rows and rows[0][0] == 0 else None
    start = responses[-turns]
    while True:
        while start > 0 and isinstance(rows[start - 1][1], ModelRequest):
            start -= 1
        if start == 0 and rows[0][0] != 0:
            return None
        closed = close_tool_pairs(rows, start)
        if closed is None:
            return None
        if closed == start:
            return rows[start][0]
        start = closed


async def protected_suffix(
    before: ReadHistoryBefore,
    *,
    next_step: NextStep,
    next_seq: int,
    after_anchor: int | None = None,
) -> HistoryRows:
    """Keep pending checkpoint requests and their tool pairs outside a new page.

    For assembly, also retain all post-anchor rows. Reading backwards avoids loading
    the closed historical prefix. The returned absolute sequences define the cut.
    """
    if next_step == "done" and (after_anchor is None or after_anchor >= next_seq - 1):
        return []
    rows: HistoryRows = []
    cursor = next_seq - 1
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
        if after_anchor is not None:
            protected = min(protected, after_anchor + 1)
        if protected < rows[0][0]:
            cursor = rows[0][0] - 1
            continue
        index = next((i for i, (seq, _) in enumerate(rows) if seq >= protected), len(rows))
        closed = close_tool_pairs(rows, index)
        if closed is not None:
            return rows[closed:]
        cursor = rows[0][0] - 1


def without_system(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """Copy borrowed values and leave the original system parts to the host."""
    copied = deepcopy(list(messages))
    for message in copied:
        if isinstance(message, ModelRequest):
            message.parts = [
                part for part in message.parts if not isinstance(part, SystemPromptPart)
            ]
    return [message for message in copied if message.parts]


async def assemble_working_context(
    *,
    session_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    next_step: NextStep,
    next_seq: int,
    page: ContextPageRecord | None,
    plugin: ContextPlugin | None,
    replay_turns: int,
) -> list[ModelMessage]:
    """Assemble system + plugin upper context + the protected current page."""
    read, before = bounded_history(session_factory, session_id, next_seq - 1)
    if page is None:
        return [message for _, message in await read()]
    if plugin is None or page.policy_key != plugin.key:
        raise ValueError(
            f"Context page policy {page.policy_key!r} does not match the context plugin"
        )
    suffix = await protected_suffix(
        before,
        next_step=next_step,
        next_seq=next_seq,
        after_anchor=page.anchor_seq,
    )
    # Older pages could include a pending checkpoint. Keep their record but fall
    # back to raw history rather than replaying its content twice or redoing on_page.
    if suffix and suffix[0][0] <= page.anchor_seq:
        return [message for _, message in await read()]
    rows: HistoryRows = []
    if replay_turns:
        cursor = page.anchor_seq
        while True:
            chunk = await before(through_seq=cursor)
            if not chunk:
                raise RuntimeError("Context page history is missing")
            rows[:0] = reversed(chunk)
            start = replay_start(rows, replay_turns)
            if start is not None:
                rows = [(seq, message) for seq, message in rows if seq >= start]
                break
            cursor = rows[0][0] - 1
    first = await read(through_seq=0)
    system = [
        part
        for _, message in first
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]
    upper = await plugin.get_context(
        ContextInput(
            ContextPage(deepcopy(page.payload)),
            deepcopy([message for _, message in rows]),
        )
    )
    return [
        *([ModelRequest(parts=deepcopy(system))] if system else []),
        *without_system(upper),
        *without_system([message for _, message in suffix]),
    ]
