"""PostgreSQL lease and history operations, borrowing one caller-owned transaction.

Checkpoint/history writes and input or cancel consumption must follow lock_owned
in the same transaction. Heartbeat and release instead enforce ownership through
their own token-qualified UPDATE statements. No method commits or manages session
lifetime. Model usage is intentionally discarded; message parts retain the SDK's
official JSON codec.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from sqlalchemy import func, insert, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from .models import AgentHistoryRow, AgentStateRow
from .types import NextStep, ResumeState, RunnerLost, SessionBusy


class AgentRepository:
    """Transaction participant; the runner owns execution and commit decisions."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

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
        return ResumeState(cast(NextStep, next_step), tuple(await self.read_history(session_id)))

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

    async def read_history(self, session_id: UUID) -> list[ModelMessage]:
        """One snapshot; existing empty state returns [], absent state raises LookupError."""
        statement = (
            select(AgentStateRow.session_id, AgentHistoryRow)
            .outerjoin(
                AgentHistoryRow, col(AgentHistoryRow.session_id) == col(AgentStateRow.session_id)
            )
            .where(col(AgentStateRow.session_id) == session_id)
            .order_by(col(AgentHistoryRow.seq))
        )
        rows = (await self._db.execute(statement)).all()
        if not rows:
            raise LookupError(str(session_id))
        payloads: list[dict[str, Any]] = []
        for _, row in rows:
            if row is None:
                continue
            payload = {**row.message_metadata, **row.message, "kind": row.kind}
            if row.kind == "response":
                payload["finish_reason"] = row.finish_reason
            payloads.append(payload)
        return ModelMessagesTypeAdapter.validate_python(payloads)

    async def save_checkpoint(
        self,
        session_id: UUID,
        *,
        next_step: NextStep,
        start_seq: int,
        messages: Sequence[ModelMessage] = (),
    ) -> None:
        """Append an explicit delta after lock_owned; the caller supplies its next seq."""
        payloads = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        rows = []
        for offset, payload in enumerate(payloads):
            kind = payload.pop("kind")
            parts = payload.pop("parts")
            payload.pop("usage", None)
            finish_reason = payload.pop("finish_reason", None)
            rows.append(
                dict(
                    session_id=session_id,
                    seq=start_seq + offset,
                    kind=kind,
                    message={"parts": parts},
                    message_metadata=payload,
                    finish_reason=finish_reason,
                )
            )
        await self._db.execute(
            update(AgentStateRow)
            .where(col(AgentStateRow.session_id) == session_id)
            .values(next_step=next_step, updated_at=func.clock_timestamp())
        )
        if rows:
            await self._db.execute(insert(AgentHistoryRow), rows)
