"""Persist runner snapshots using process-owned resources outside Workflow code."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from kapy.control.sessions.repository import SessionRepository

from .types import SaveRunnerStateInput


class RunnerStateActivities:
    """Each invocation owns a short transaction; the Worker owns the session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
