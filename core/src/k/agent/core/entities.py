"""Shared Pydantic models for the agent core.

Keep these types isolated from the agent wiring so they can be imported by
tools, deps, and runners without creating import cycles.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable
from logging import getLogger
from typing import Protocol, cast

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from k.agent.memory.entities import is_memory_record_id
from k.agent.memory.store import MemoryStore

logger = getLogger(__name__)


class Event(BaseModel):
    kind: str
    content: str


class MemoryHint(BaseModel):
    referenced_memory_ids: list[str]
    from_where_and_response_to_where: str
    user_intents: str


class _FinishActionDeps(Protocol):
    """Minimum deps contract required by `finish_action` validation."""

    memory_storage: MemoryStore


def _validate_referenced_memory_ids(
    *,
    memory_store: MemoryStore,
    referenced_memory_ids: list[str],
) -> list[str]:
    """Validate referenced memory IDs emitted by `finish_action`.

    Raises:
        ModelRetry: If any id is malformed or does not exist in `memory_store`.
    """

    invalid_ids = [
        mem_id for mem_id in referenced_memory_ids if not is_memory_record_id(mem_id)
    ]
    if invalid_ids:
        raise ModelRetry(
            "Invalid referenced_memory_ids: each id must be a valid MemoryRecord id. "
            f"Invalid id(s): {invalid_ids}"
        )

    missing_ids = [
        mem_id
        for mem_id in referenced_memory_ids
        if memory_store.get_by_id(mem_id) is None
    ]
    if missing_ids:
        raise ModelRetry(
            "Unknown referenced_memory_ids: each id must exist in the current memory store. "
            f"Missing id(s): {missing_ids}"
        )

    return list(referenced_memory_ids)


def tool_exception_guard[**P, R](
    fn: Callable[P, Awaitable[R] | R],
) -> Callable[P, Awaitable[R | str]]:
    """Decorator for tool functions that converts unexpected failures to strings.

    - Lets `asyncio.CancelledError` propagate (cancellation should abort).
    - Catches all other `Exception` instances and returns `str(e)`.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | str:
        try:
            res = fn(*args, **kwargs)
            if inspect.isawaitable(res):
                return await cast(Awaitable[R], res)
            return cast(R, res)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            tool_name = getattr(fn, "__name__", type(fn).__name__)
            logger.info(f"Exception in tool {tool_name}: {exc}", exc_info=True)
            return str(exc)

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


def finish_action(
    ctx: RunContext[_FinishActionDeps],
    referenced_memory_ids: list[str],
    from_where_and_response_to_where: str,
    user_intents: str,
) -> MemoryHint:
    """
    Final structured output for the agent run

    Args:
        referenced_memory_ids: Memory record IDs that were used as context. Can be empty.
            Must be valid, existing memory ids.
        from_where_and_response_to_where: Description of the input sources and response destinations (channel, app, IDs, etc.).
        user_intents: The interpreted intent(s) of the user. If there are multiple intents,
            include them all (e.g. as a short numbered/bulleted list in one string).
    """

    validated_ids = _validate_referenced_memory_ids(
        memory_store=ctx.deps.memory_storage,
        referenced_memory_ids=referenced_memory_ids,
    )
    return MemoryHint(
        referenced_memory_ids=validated_ids,
        from_where_and_response_to_where=from_where_and_response_to_where,
        user_intents=user_intents,
    )
