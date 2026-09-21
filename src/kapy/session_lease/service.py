"""PostgreSQL ownership without a pinned connection or long-running transaction.

The database clock determines takeover eligibility. Expiration alone does not
revoke a token: renewal and takeover serialize on the lease row. A replaced owner
cannot commit writes fenced by lock_owned, but already-issued external requests
may still finish. External effects need idempotency or their own fencing.
"""

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import anyio
from sqlalchemy import func, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col, select

from .models import SessionLeaseRow


class SessionBusy(RuntimeError):
    """Another operation still owns a live session lease."""


class LeaseLost(RuntimeError):
    """Ownership was replaced, or the borrowed lease handle is no longer valid."""


class SessionLease:
    """One operation's ownership; obtain through open_session_lease.

    Children may borrow this handle only within the owner's scope, using separate
    AsyncSessions. Known failure is permanent. check does not query PostgreSQL;
    protected writes require lock_owned in the same transaction as those writes.
    """

    def __init__(self, session_id: UUID, token: UUID) -> None:
        self.session_id = session_id
        self._token = token
        self._active = True
        self._error: BaseException | None = None
        self._lost = asyncio.Event()

    def check(self) -> None:
        """Reject an expired handle or known failure without accessing the database."""
        if self._error is not None:
            raise self._error
        if not self._active:
            raise LeaseLost(str(self.session_id))

    async def lock_owned(self, db: AsyncSession) -> None:
        """Lock and check ownership in the caller's transaction; never commit it.

        Use the same database/schema and READ COMMITTED isolation as acquisition.
        Take this lock before business row locks. All protected writes must follow
        in this transaction: a check in an earlier transaction gives no protection.
        This cooperative protocol cannot intercept SQL that bypasses it.
        """
        self.check()
        try:
            owned = (
                await db.execute(
                    select(SessionLeaseRow.session_id)
                    .where(
                        col(SessionLeaseRow.session_id) == self.session_id,
                        col(SessionLeaseRow.lock_token) == self._token,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if owned is None:
                raise LeaseLost(str(self.session_id))
            self.check()
        except Exception as error:
            self._remember(error)
            raise

    async def wait_lost(self) -> None:
        """Wait and raise a known failure; callers own and must join their monitors."""
        self.check()
        await self._lost.wait()
        self.check()

    def _remember(self, error: BaseException) -> None:
        if self._error is None:
            self._error = error
        elif self._error is not error:
            self._error.add_note(f"Additional lease failure: {error!r}")
        self._lost.set()


async def _acquire(
    factory: async_sessionmaker[AsyncSession], lease: SessionLease, heartbeat_timeout: float
) -> None:
    statement = (
        insert(SessionLeaseRow)
        .values(session_id=lease.session_id, lock_token=lease._token)
        .on_conflict_do_update(
            index_elements=["session_id"],
            set_={"lock_token": lease._token, "heartbeat_at": func.clock_timestamp()},
            where=or_(
                col(SessionLeaseRow.lock_token).is_(None),
                col(SessionLeaseRow.heartbeat_at)
                <= func.clock_timestamp() - timedelta(seconds=heartbeat_timeout),
            ),
        )
        .returning(col(SessionLeaseRow.session_id))
    )
    # Explicit transaction calls keep commit/rollback in this task. The maker's
    # begin context shields a separate commit task which could outlive cancellation.
    db = factory()
    try:
        if (await db.execute(statement)).scalar_one_or_none() is None:
            raise SessionBusy(str(lease.session_id))
        await db.commit()
    finally:
        await db.close()


async def _abort_acquisition(task: asyncio.Task[None]) -> None:
    """Cancel and join the actual transaction, interrupting stalled driver cleanup."""
    task.cancel()
    try:
        async with asyncio.timeout(5):
            await task
    except asyncio.CancelledError:
        # The transaction's cancellation is expected. A timeout cancels it again
        # and joins its unwind; no detached commit exists in _acquire.
        pass


async def _join_protected(task: asyncio.Task[None]) -> asyncio.CancelledError | None:
    """Join a DB-only task despite caller cancellation; the caller reads its result."""
    cancellation = None
    # AnyIO cancellation is level-triggered; raw asyncio cancellation is
    # edge-triggered. Shield/join for both without losing either signal.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as cancelled:
                if cancellation is None:
                    cancellation = cancelled
            except BaseException:
                break
    try:
        # Retain scope cancellation even when no asyncio cancellation was
        # delivered while the DB-only task was shielded.
        await anyio.lowlevel.checkpoint_if_cancelled()
    except asyncio.CancelledError as cancelled:
        if cancellation is None:
            cancellation = cancelled
    return cancellation


async def _update_owned(db: AsyncSession, lease: SessionLease, *, release: bool) -> None:
    values = {"lock_token": None} if release else {"heartbeat_at": func.clock_timestamp()}
    statement = (
        update(SessionLeaseRow)
        .where(
            col(SessionLeaseRow.session_id) == lease.session_id,
            col(SessionLeaseRow.lock_token) == lease._token,
        )
        .values(**values)
        .returning(col(SessionLeaseRow.session_id))
    )
    if (await db.execute(statement)).scalar_one_or_none() is None:
        raise LeaseLost(str(lease.session_id))


async def _heartbeat(
    lease: SessionLease, factory: async_sessionmaker[AsyncSession], interval: float
) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            async with factory.begin() as db:
                await _update_owned(db, lease, release=False)
    except Exception as error:
        # Report failure without implicitly cancelling external model/tool work.
        lease._remember(error)


async def _cleanup(
    lease: SessionLease,
    heartbeat: asyncio.Task[None] | None,
    factory: async_sessionmaker[AsyncSession],
    *,
    release_required: bool,
) -> None:
    # Cleanup alone is bounded; callers finish their work and resources before
    # exiting the context. It is safe to run this DB-only cleanup in another task.
    async with asyncio.timeout(5):
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        async with factory.begin() as db:
            try:
                await _update_owned(db, lease, release=True)
            except LeaseLost:
                if release_required:
                    raise


@asynccontextmanager
async def open_session_lease(
    session_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    heartbeat_interval: float = 10.0,
    heartbeat_timeout: float = 60.0,
) -> AsyncIterator[SessionLease]:
    """Acquire ownership, renew it through cleanup, then conditionally release.

    Enter/exit in the same task. Do not nest acquisition of the same session:
    internal operations borrow this handle. Join all children and loss monitors,
    and finish resource cleanup before exiting; children use separate DB sessions.
    The engine/pool belongs to the application. All participants must use the same
    finite policy, with 0 < heartbeat_interval < heartbeat_timeout (seconds).

    A live owner causes SessionBusy; acquisition does not poll or automatically
    retry. Database failures retain their original exception types.

    Failure is reported at check/lock_owned/wait_lost and normal context exit.
    A foreground exception, including cancellation, stays primary; simultaneous
    lease/cleanup failures are attached as exception notes. Cancellation during
    acquisition cancels and joins its transaction, with a five-second driver
    cleanup deadline, before conditionally releasing any committed token. No handle
    is yielded to the cancelled caller. Cleanup is protected
    from caller cancellation for at most five seconds; cancellation still escapes.
    Neither this context nor timeout can undo external effects or ensure exactly-once.
    """
    if not (
        math.isfinite(heartbeat_interval)
        and math.isfinite(heartbeat_timeout)
        and 0 < heartbeat_interval < heartbeat_timeout
    ):
        raise ValueError("Require finite 0 < heartbeat_interval < heartbeat_timeout")
    lease = SessionLease(session_id, uuid4())
    acquired = False
    acquisition = None
    heartbeat = None
    error: BaseException | None = None
    try:
        # Cancellation must stop lock waits while still joining the transaction
        # before release. _acquire uses no context that detaches commit or close.
        acquisition = asyncio.create_task(_acquire(session_factory, lease, heartbeat_timeout))
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError as cancelled:
            abort = asyncio.create_task(_abort_acquisition(acquisition))
            await _join_protected(abort)
            try:
                abort.result()
            except BaseException as abort_error:
                cancelled.add_note(f"Lease acquisition abort failed: {abort_error!r}")
            raise
        acquired = True
        coroutine = _heartbeat(lease, session_factory, heartbeat_interval)
        try:
            heartbeat = asyncio.create_task(coroutine, name=f"session-heartbeat:{session_id}")
        except BaseException:
            coroutine.close()
            raise
        yield lease
        lease.check()
    except BaseException as caught:
        error = caught
    finally:
        lease._active = False
        lease._lost.set()
        if acquisition is not None:
            # A cancelled/failed COMMIT can have an uncertain server outcome.
            # Only after its task has ended may we release this token; a missing
            # token is expected when acquisition rolled back or never succeeded.
            cleanup = asyncio.create_task(
                _cleanup(lease, heartbeat, session_factory, release_required=acquired)
            )
            cancellation = await _join_protected(cleanup)
            if error is None:
                error = cancellation
            try:
                cleanup.result()
            except BaseException as cleanup_error:
                lease._remember(cleanup_error)
        if lease._error is not None and error is not lease._error:
            if error is None:
                error = lease._error
            else:
                error.add_note(f"Lease failure: {lease._error!r}")
                for note in getattr(lease._error, "__notes__", ()):
                    error.add_note(note)
        if error is not None:
            raise error


async def is_session_busy(
    db: AsyncSession, session_id: UUID, *, heartbeat_timeout: float = 60.0
) -> bool:
    """Observe live ownership, without reserving it or judging business progress."""
    if not math.isfinite(heartbeat_timeout) or heartbeat_timeout <= 0:
        raise ValueError("heartbeat_timeout must be finite and positive")
    statement = select(
        select(SessionLeaseRow.session_id)
        .where(
            col(SessionLeaseRow.session_id) == session_id,
            col(SessionLeaseRow.lock_token).is_not(None),
            col(SessionLeaseRow.heartbeat_at)
            > func.clock_timestamp() - timedelta(seconds=heartbeat_timeout),
        )
        .exists()
    )
    return (await db.execute(statement)).scalar_one()
