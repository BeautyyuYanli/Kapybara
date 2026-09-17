"""Shared business pages and bounded queries; callers own ordering and transactions."""

from collections.abc import Callable, Sequence
from typing import Annotated

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

type PageLimit = Annotated[int, Field(ge=1, le=200, strict=True)]


class Page[T](BaseModel):
    items: list[T]
    has_more: bool


class OffsetPagination(BaseModel):
    offset: int = Field(default=0, ge=0, strict=True)
    limit: PageLimit = 100


class BeforeSeqPagination(BaseModel):
    before_seq: int | None = Field(default=None, ge=0, strict=True)
    limit: PageLimit = 100


def validate_pagination(offset: int, limit: int) -> None:
    OffsetPagination(offset=offset, limit=limit)


async def paginate[RowT, ItemT](
    db: AsyncSession,
    statement: Select[tuple[RowT]],
    *,
    limit: int,
    decode_rows: Callable[[Sequence[RowT]], Sequence[ItemT]],
) -> Page[ItemT]:
    """Read one extra row, trim before decoding, and avoid a separate COUNT query."""
    rows = (await db.execute(statement.limit(limit + 1))).scalars().all()
    return Page(items=list(decode_rows(rows[:limit])), has_more=len(rows) > limit)
