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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from k.agent.channels import (
    effective_out_channel,
    normalize_out_channel,
    validate_channel_path,
    validate_contact_path,
)
from k.agent.memory.store import MemoryStore

logger = getLogger(__name__)


class Event(BaseModel):
    """Structured input event with hierarchical channel routing.

    Required fields:
    - `in_channel`: source channel path.
    Optional fields:
    - `contacts`: source user identity paths in `<platform>/<user_id>` form.
    """

    model_config = ConfigDict(extra="forbid")

    in_channel: str
    contacts: list[str] = Field(default_factory=list)
    out_channel: str | None = None
    content: str

    @field_validator("in_channel")
    @classmethod
    def _validate_in_channel(cls, value: str) -> str:
        return validate_channel_path(value, field_name="in_channel")

    @field_validator("contacts")
    @classmethod
    def _validate_contacts(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for idx, contact in enumerate(value):
            normalized_contact = validate_contact_path(
                contact,
                field_name=f"contacts[{idx}]",
            )
            if normalized_contact in seen:
                continue
            seen.add(normalized_contact)
            normalized.append(normalized_contact)
        return normalized

    @field_validator("out_channel")
    @classmethod
    def _validate_out_channel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_channel_path(value, field_name="out_channel")

    @model_validator(mode="after")
    def _normalize_out_channel(self) -> Event:
        self.out_channel = normalize_out_channel(
            in_channel=self.in_channel,
            out_channel=self.out_channel,
        )
        return self

    @property
    def effective_out_channel(self) -> str:
        return effective_out_channel(
            in_channel=self.in_channel,
            out_channel=self.out_channel,
        )


class MemoryHint(BaseModel):
    referenced_memory_ids: list[str]
    from_where_and_response_to_where: str
    user_intents: str


class _FinishActionDeps(Protocol):
    """Minimum deps contract required by `finish_action` validation."""

    memory_storage: MemoryStore


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
