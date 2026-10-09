"""Runner history and checkpoints, borrowing one caller-owned transaction.

Legacy checkpoint mutations follow SessionLease.lock_owned in the same
transaction. Temporal append_history seals all stored positions: its caller
locks the session row in the same transaction, without using legacy leases.
No method acquires a lease, commits, or manages session lifetime. Usage is
normalized outside message JSON; message parts retain the SDK's official codec.
"""

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from sqlalchemy import func, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.pagination import Page, paginate

from .models import AgentContextPageRow, AgentHistoryRow, AgentStateRow
from .types import ContextPageRecord, HistoryMessage, NextStep, ResumeState


def response_tokens(response: ModelResponse) -> tuple[int, int] | None:
    """Zero/unknown input usage is not a usable context-size observation."""
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


class AgentRepository:
    """Transaction participant; the runner owns execution and commit decisions."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def resume(self, session_id: UUID) -> ResumeState:
        """Initialize/read checkpoint metadata after lease.lock_owned in this transaction."""
        await self._db.execute(
            pg_insert(AgentStateRow)
            .values(session_id=session_id, next_step="done")
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        next_step = (
            await self._db.execute(
                select(AgentStateRow.next_step).where(col(AgentStateRow.session_id) == session_id)
            )
        ).scalar_one()
        last_seq = (
            await self._db.execute(
                select(AgentHistoryRow.seq)
                .where(col(AgentHistoryRow.session_id) == session_id)
                .order_by(col(AgentHistoryRow.seq).desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return ResumeState(
            cast(NextStep, next_step),
            0 if last_seq is None else last_seq + 1,
            await self.read_latest_page(session_id),
        )

    async def read_history(
        self, session_id: UUID, *, start_seq: int, through_seq: int
    ) -> list[tuple[int, ModelMessage]]:
        """Read the inclusive range in ascending absolute seq order; empty ranges return []."""
        statement = (
            select(AgentHistoryRow)
            .where(
                col(AgentHistoryRow.session_id) == session_id,
                col(AgentHistoryRow.seq) >= start_seq,
                col(AgentHistoryRow.seq) <= through_seq,
            )
            .order_by(col(AgentHistoryRow.seq))
        )
        return [
            (entry.seq, entry.message)
            for entry in _decode_history((await self._db.execute(statement)).scalars().all())
        ]

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

    async def read_history_before(
        self, session_id: UUID, *, through_seq: int, limit: int
    ) -> list[tuple[int, ModelMessage]]:
        """Read at most limit rows at/before through_seq, in descending seq order."""
        statement = (
            select(AgentHistoryRow)
            .where(
                col(AgentHistoryRow.session_id) == session_id,
                col(AgentHistoryRow.seq) <= through_seq,
            )
            .order_by(col(AgentHistoryRow.seq).desc())
            .limit(limit)
        )
        return [
            (entry.seq, entry.message)
            for entry in _decode_history((await self._db.execute(statement)).scalars().all())
        ]

    async def read_latest_page(self, session_id: UUID) -> ContextPageRecord | None:
        row = (
            await self._db.execute(
                select(AgentContextPageRow)
                .where(col(AgentContextPageRow.session_id) == session_id)
                .order_by(col(AgentContextPageRow.anchor_seq).desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return (
            None if row is None else ContextPageRecord(row.anchor_seq, row.policy_key, row.payload)
        )

    async def save_page(self, session_id: UUID, page: ContextPageRecord) -> ContextPageRecord:
        """Insert one immutable page after lock_owned in the caller's transaction."""
        await self._db.execute(
            insert(AgentContextPageRow).values(
                session_id=session_id,
                anchor_seq=page.anchor_seq,
                policy_key=page.policy_key,
                payload=page.payload,
            )
        )
        return page

    async def read_latest_response(
        self, session_id: UUID, *, through_seq: int
    ) -> HistoryMessage | None:
        """Restore usage without requiring the assembler to retain an old response."""
        rows = (
            (
                await self._db.execute(
                    select(AgentHistoryRow)
                    .where(
                        col(AgentHistoryRow.session_id) == session_id,
                        col(AgentHistoryRow.seq) <= through_seq,
                        col(AgentHistoryRow.kind) == "response",
                    )
                    .order_by(col(AgentHistoryRow.seq).desc())
                    .limit(1)
                )
            )
            .scalars()
            .all()
        )
        return _decode_history(rows)[0] if rows else None

    async def save_checkpoint(
        self,
        session_id: UUID,
        *,
        next_step: NextStep,
        start_seq: int,
        messages: Sequence[ModelMessage] = (),
    ) -> tuple[HistoryMessage, ...]:
        """Append after lock_owned and return normalized DTOs, without committing.

        DTOs derive from the INSERT payload, without another database read. They
        must only be published after the caller's transaction has committed.
        """
        rows = _encode_history(
            [
                HistoryMessage(session_id, start_seq + offset, message)
                for offset, message in enumerate(messages)
            ]
        )
        await self._db.execute(
            update(AgentStateRow)
            .where(col(AgentStateRow.session_id) == session_id)
            .values(next_step=next_step, updated_at=func.clock_timestamp())
        )
        if rows:
            await self._db.execute(insert(AgentHistoryRow), rows)
        return _decode_history([AgentHistoryRow(**row) for row in rows])

    async def append_history(
        self, session_id: UUID, messages: Sequence[HistoryMessage]
    ) -> tuple[HistoryMessage, ...]:
        """Append above the stored maximum and return new DTOs in input order.

        The caller holds the session row lock through commit. All positions in one
        batch use the same pre-write maximum; old positions and their numeric gaps
        are permanently sealed. JSON metadata never determines stored positions.
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
        old_boundary = -1 if last_seq is None else last_seq
        accepted = tuple(message for message in messages if message.seq > old_boundary)
        rows = _encode_history(accepted)
        if rows:
            await self._db.execute(insert(AgentHistoryRow), rows)
        return accepted
