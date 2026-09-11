"""PostgreSQL lease and history operations, borrowing one caller-owned transaction.

Checkpoint/history writes and input or cancel consumption must follow lock_owned
in the same transaction. Heartbeat and release instead enforce ownership through
their own token-qualified UPDATE statements. No method commits or manages session
lifetime. Only normalized input/output usage is stored, outside message JSON;
message parts retain the SDK's official JSON codec.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from sqlalchemy import func, insert, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from .models import AgentCompactionRow, AgentHistoryRow, AgentStateRow
from .types import Compaction, HistoryMessage, NextStep, ResumeState, RunnerLost, SessionBusy


def response_tokens(response: ModelResponse) -> tuple[int, int] | None:
    """Zero/unknown input usage is not a usable context-size observation."""
    usage = response.usage
    if usage.input_tokens > 0 and usage.output_tokens >= 0:
        return usage.input_tokens, usage.output_tokens
    return None


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

    async def is_runner_running(self, session_id: UUID, *, heartbeat_timeout: float) -> bool:
        """Observe the current live lease without locking or renewing it.

        This uses the same database clock/timeout as acquire. It neither proves
        process liveness nor reserves execution; callers still need acquire.
        """
        statement = select(
            select(AgentStateRow.session_id)
            .where(
                col(AgentStateRow.session_id) == session_id,
                col(AgentStateRow.lock_token).is_not(None),
                col(AgentStateRow.heartbeat_at)
                > func.clock_timestamp() - timedelta(seconds=heartbeat_timeout),
            )
            .exists()
        )
        return (await self._db.execute(statement)).scalar_one()

    async def acquire(
        self, session_id: UUID, lock_token: UUID, *, heartbeat_timeout: float
    ) -> ResumeState:
        statement = (
            pg_insert(AgentStateRow)
            .values(session_id=session_id, lock_token=lock_token, next_step="done")
            .on_conflict_do_update(
                index_elements=["session_id"],
                set_={"lock_token": lock_token, "heartbeat_at": func.clock_timestamp()},
                where=or_(
                    col(AgentStateRow.lock_token).is_(None),
                    col(AgentStateRow.heartbeat_at)
                    <= func.clock_timestamp() - timedelta(seconds=heartbeat_timeout),
                ),
            )
            .returning(col(AgentStateRow.next_step))
        )
        next_step = (await self._db.execute(statement)).scalar_one_or_none()
        if next_step is None:
            raise SessionBusy(str(session_id))
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
            await self.read_latest_compaction(session_id),
        )

    async def lock_owned(self, session_id: UUID, lock_token: UUID) -> None:
        """Wait for the row lock, then fail with RunnerLost if token no longer matches."""
        statement = (
            select(col(AgentStateRow.session_id))
            .where(
                col(AgentStateRow.session_id) == session_id,
                col(AgentStateRow.lock_token) == lock_token,
            )
            .with_for_update()
        )
        if (await self._db.execute(statement)).scalar_one_or_none() is None:
            raise RunnerLost(str(session_id))

    async def heartbeat(self, session_id: UUID, lock_token: UUID) -> None:
        statement = (
            update(AgentStateRow)
            .where(
                col(AgentStateRow.session_id) == session_id,
                col(AgentStateRow.lock_token) == lock_token,
            )
            .values(heartbeat_at=func.clock_timestamp())
            .returning(col(AgentStateRow.session_id))
        )
        if (await self._db.execute(statement)).scalar_one_or_none() is None:
            raise RunnerLost(str(session_id))

    async def release(self, session_id: UUID, lock_token: UUID) -> None:
        await self._db.execute(
            update(AgentStateRow)
            .where(
                col(AgentStateRow.session_id) == session_id,
                col(AgentStateRow.lock_token) == lock_token,
            )
            .values(lock_token=None)
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
        """Read original history after an absolute cursor, including for unstarted sessions."""
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

    async def read_latest_compaction(self, session_id: UUID) -> Compaction | None:
        row = (
            await self._db.execute(
                select(AgentCompactionRow)
                .where(col(AgentCompactionRow.session_id) == session_id)
                .order_by(col(AgentCompactionRow.last_message_seq).desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else Compaction(row.last_message_seq, row.text)

    async def save_compaction(
        self, session_id: UUID, *, last_message_seq: int, text: str
    ) -> Compaction:
        """Insert one immutable summary after lock_owned in the caller's transaction."""
        await self._db.execute(
            insert(AgentCompactionRow).values(
                session_id=session_id, last_message_seq=last_message_seq, text=text
            )
        )
        return Compaction(last_message_seq, text)

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
        payloads = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        rows: list[dict[str, Any]] = []
        for offset, payload in enumerate(payloads):
            kind = payload.pop("kind")
            parts = payload.pop("parts")
            payload.pop("usage", None)
            finish_reason = payload.pop("finish_reason", None)
            message = messages[offset]
            tokens = response_tokens(message) if isinstance(message, ModelResponse) else None
            rows.append(
                dict(
                    session_id=session_id,
                    seq=start_seq + offset,
                    kind=kind,
                    message={"parts": parts},
                    message_metadata=payload,
                    finish_reason=finish_reason,
                    input_tokens=tokens[0] if tokens is not None else None,
                    output_tokens=tokens[1] if tokens is not None else None,
                )
            )
        await self._db.execute(
            update(AgentStateRow)
            .where(col(AgentStateRow.session_id) == session_id)
            .values(next_step=next_step, updated_at=func.clock_timestamp())
        )
        if rows:
            await self._db.execute(insert(AgentHistoryRow), rows)
        return _decode_history([AgentHistoryRow(**row) for row in rows])
