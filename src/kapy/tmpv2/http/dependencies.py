"""HTTP parameter sources; business pagination constraints live in tmpv2.pagination."""

from typing import Annotated

from fastapi import Depends, Query

from kapy.tmpv2.pagination import BeforeSeqPagination, OffsetPagination


def offset_pagination(offset: int = 0, limit: int = 100) -> OffsetPagination:
    return OffsetPagination(offset=offset, limit=limit)


def history_pagination(before_seq: int | None = None, limit: int = 100) -> BeforeSeqPagination:
    return BeforeSeqPagination(before_seq=before_seq, limit=limit)


type OffsetPage = Annotated[OffsetPagination, Depends(offset_pagination)]
type HistoryPage = Annotated[BeforeSeqPagination, Depends(history_pagination)]
type LiveCursor = Annotated[int, Query(ge=-1)]
