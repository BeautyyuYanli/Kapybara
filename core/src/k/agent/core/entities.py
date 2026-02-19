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
from typing import cast

from pydantic import BaseModel

logger = getLogger(__name__)


class Event(BaseModel):
    kind: str
    content: str


class MemoryHint(BaseModel):
    referenced_memory_ids: list[str]
    from_where_and_response_to_where: str
    user_intents: str


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
    referenced_memory_ids: list[str],
    from_where_and_response_to_where: str,
    user_intents: str,
) -> MemoryHint:
    """
    Final structured output for the agent run

    Args:
        referenced_memory_ids: Memory record IDs that were used as context. Can be empty.
        from_where_and_response_to_where: Description of the input sources and response destinations (channel, app, IDs, etc.).
        user_intents: The interpreted intent(s) of the user. If there are multiple intents,
            include them all (e.g. as a short numbered/bulleted list in one string).
    """

    return MemoryHint(
        referenced_memory_ids=referenced_memory_ids,
        from_where_and_response_to_where=from_where_and_response_to_where,
        user_intents=user_intents,
    )
