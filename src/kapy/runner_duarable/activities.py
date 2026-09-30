"""Persist runner snapshots using process-owned resources outside Workflow code."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col, select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from kapy.agent_output import AgentOutputService
from kapy.agent_runner.repository import AgentRepository
from kapy.agent_runner.types import MessageCommitted
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository

from .types import MessageBatch, SaveRunnerStateInput


class RunnerActivities:
    """Each invocation owns a short transaction; the Worker owns the session factory."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], output_service: AgentOutputService
    ) -> None:
        self._session_factory = session_factory
        self._output_service = output_service

    @activity.defn(name="kapy.record_history")
    async def record_messages(self, data: MessageBatch) -> None:
        """Serialize session writes and publish accepted snapshots only after commit.

        Confirmed retries are discarded; missed broadcasts are recovered from history.
        Lock only the session id, avoiding a read of the potentially large runner state.
        """
        try:
            async with self._session_factory.begin() as db:
                session_id = (
                    await db.execute(
                        select(SessionRow.id)
                        .where(col(SessionRow.id) == data.session_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if session_id is None:
                    raise LookupError(f"Session {data.session_id} does not exist")
                accepted = await AgentRepository(db).upsert_history(session_id, data.messages)
        except LookupError as exc:
            raise ApplicationError(str(exc), type="SessionNotFound", non_retryable=True) from exc
        except ValueError as exc:
            raise ApplicationError(str(exc), type="InvalidHistory", non_retryable=True) from exc
        if not accepted:
            return
        async with self._output_service.publisher(data.session_id) as publish:
            for message in accepted:
                await publish(MessageCommitted(message))

    @activity.defn(name="kapy.save_runner_state")
    async def save_runner_state(self, data: SaveRunnerStateInput) -> None:
        """Commit before acknowledging; duplicate execution uses repository idempotency."""
        try:
            async with self._session_factory.begin() as db:
                await SessionRepository(db).save_runner_state(
                    data.session_id,
                    expected_version=data.expected_version,
                    runner_state=data.runner_state,
                )
        except LookupError as exc:
            raise ApplicationError(str(exc), type="SessionNotFound", non_retryable=True) from exc
        except ValueError as exc:
            raise ApplicationError(
                str(exc), type="RunnerStateConflict", non_retryable=True
            ) from exc
