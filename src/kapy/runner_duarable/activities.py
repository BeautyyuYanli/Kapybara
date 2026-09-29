"""Persist runner snapshots using process-owned resources outside Workflow code."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from kapy.agent_output import AgentOutputService
from kapy.agent_runner.repository import AgentRepository
from kapy.agent_runner.types import MessageCommitted
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
        """Commit before publishing complete input snapshots; retries may broadcast again."""
        try:
            async with self._session_factory.begin() as db:
                await AgentRepository(db).upsert_history(data.session_id, data.messages)
        except ValueError as exc:
            raise ApplicationError(str(exc), type="InvalidHistory", non_retryable=True) from exc
        async with self._output_service.publisher(data.session_id) as publish:
            for message in data.messages:
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
