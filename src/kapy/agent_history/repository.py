"""Immutable history in a caller-owned transaction, using the official SDK codec.

Writers serialize with a conditional session-ownership write in the same transaction.
This repository never claims operation ownership,
commits, or manages session lifetime. Usage is normalized outside message JSON.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.pagination import Page, paginate

from .models import AgentHistoryRow
from .types import HistoryMessage


def response_tokens(response: ModelResponse) -> tuple[int, int] | None:
    """Store usable positive input and nonnegative output counts outside SDK JSON."""
    usage = response.usage
    if usage.input_tokens > 0 and usage.output_tokens >= 0:
        return usage.input_tokens, usage.output_tokens
    return None


def _encode_history(messages: Sequence[HistoryMessage]) -> list[dict[str, Any]]:
    payloads = ModelMessagesTypeAdapter.dump_python(
        [entry.message for entry in messages], mode="json"
    )
    rows: list[dict[str, Any]] = []
    for entry, payload in zip(messages, payloads, strict=True):
        kind = payload.pop("kind")
        parts = payload.pop("parts")
        payload.pop("usage", None)
        finish_reason = payload.pop("finish_reason", None)
        tokens = (
            response_tokens(entry.message) if isinstance(entry.message, ModelResponse) else None
        )
        rows.append(
            dict(
                session_id=entry.session_id,
                seq=entry.seq,
                kind=kind,
                message={"parts": parts},
                message_metadata=payload,
                finish_reason=finish_reason,
                input_tokens=tokens[0] if tokens is not None else None,
                output_tokens=tokens[1] if tokens is not None else None,
            )
        )
    return rows


def _decode_history(rows: Sequence[AgentHistoryRow]) -> tuple[HistoryMessage, ...]:
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = {**row.message_metadata, **row.message, "kind": row.kind}
        if row.kind == "response":
            payload["finish_reason"] = row.finish_reason
            if row.input_tokens is not None and row.output_tokens is not None:
                payload["usage"] = dict(
                    input_tokens=row.input_tokens, output_tokens=row.output_tokens
                )
        payloads.append(payload)
    messages = ModelMessagesTypeAdapter.validate_python(payloads)
    return tuple(
        HistoryMessage(row.session_id, row.seq, message)
        for row, message in zip(rows, messages, strict=True)
    )


def history_projection(message: ModelMessage) -> dict[str, Any]:
    """Compare complete SDK payloads using exactly the stored usage projection."""
    payload = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
    if isinstance(message, ModelResponse):
        payload.pop("usage", None)
        payload["tokens"] = response_tokens(message)
    return payload


class HistoryRepository:
    """Append and replay participant; callers own locking and commit decisions."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def read_history_entries(
        self, session_id: UUID, *, after_seq: int = -1
    ) -> tuple[HistoryMessage, ...]:
        """Read immutable complete messages strictly after the seq cursor, in ascending order."""
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < -1:
            raise ValueError("after_seq must be an integer >= -1")
        statement = (
            select(AgentHistoryRow)
            .where(
                col(AgentHistoryRow.session_id) == session_id,
                col(AgentHistoryRow.seq) > after_seq,
            )
            .order_by(col(AgentHistoryRow.seq))
        )
        return _decode_history((await self._db.execute(statement)).scalars().all())

    async def read_history_page(
        self, session_id: UUID, *, before_seq: int | None, limit: int
    ) -> Page[HistoryMessage]:
        """Latest matching window, returned ascending; has_more means older rows exist."""
        statement = select(AgentHistoryRow).where(col(AgentHistoryRow.session_id) == session_id)
        if before_seq is not None:
            statement = statement.where(col(AgentHistoryRow.seq) < before_seq)
        return await paginate(
            self._db,
            statement.order_by(col(AgentHistoryRow.seq).desc()),
            limit=limit,
            decode_rows=lambda rows: list(reversed(_decode_history(rows))),
        )

    async def append_history(
        self, session_id: UUID, messages: Sequence[HistoryMessage]
    ) -> tuple[HistoryMessage, ...]:
        """Append above the stored maximum and return new DTOs in input order.

        The caller serializes through its conditional session-ownership write in
        this transaction. All positions in one batch use the same pre-write maximum;
        old positions and their numeric gaps stay sealed. JSON metadata never
        determines stored positions.
        Invalid, duplicate or cross-session positions reject the whole batch; an
        empty batch does nothing. Only publish returned DTOs after committing.
        """
        seen: set[int] = set()
        for message in messages:
            seq = message.seq
            if type(seq) is not int or seq < 0:
                raise ValueError("History seq must be a nonnegative integer")
            if message.session_id != session_id:
                raise ValueError("History message belongs to another session")
            if seq in seen:
                raise ValueError("A history batch cannot contain duplicate seq values")
            seen.add(seq)
        if not messages:
            return ()
        old_boundary = await self.max_seq(session_id)
        accepted = tuple(message for message in messages if message.seq > old_boundary)
        rows = _encode_history(accepted)
        if rows:
            await self._db.execute(insert(AgentHistoryRow), rows)
        return accepted

    async def max_seq(self, session_id: UUID) -> int:
        """Read the sealed boundary in the caller's ownership transaction."""
        last_seq = (
            await self._db.execute(
                select(AgentHistoryRow.seq)
                .where(
                    col(AgentHistoryRow.session_id) == session_id,
                )
                .order_by(col(AgentHistoryRow.seq).desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return -1 if last_seq is None else last_seq
