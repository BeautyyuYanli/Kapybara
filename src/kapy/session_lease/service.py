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
from sqlalchemy import func, update
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

    def _remember(self, error: BaseException) -> None:
        if self._error is None:
            self._error = error


async def _acquire(
    factory: async_sessionmaker[AsyncSession], lease: SessionLease, heartbeat_timeout: float
) -> bool:
    """Commit ownership and return whether an expired, nonempty token was replaced."""
    # Explicit transaction calls keep commit/rollback in this task. The maker's
    # begin context shields a separate commit task which could outlive cancellation.
    db = factory()
    try:
        inserted = (
            await db.execute(
                insert(SessionLeaseRow)
                .values(session_id=lease.session_id, lock_token=lease._token)
                .on_conflict_do_nothing()
                .returning(col(SessionLeaseRow.session_id))
            )
        ).scalar_one_or_none()
        takeover = False
        if inserted is None:
            token, expired = (
                await db.execute(
                    select(
                        SessionLeaseRow.lock_token,
                        col(SessionLeaseRow.heartbeat_at)
                        <= func.clock_timestamp() - timedelta(seconds=heartbeat_timeout),
                    )
                    .where(col(SessionLeaseRow.session_id) == lease.session_id)
                    .with_for_update()
                )
            ).one()
            if token is not None and not expired:
                raise SessionBusy(str(lease.session_id))
            takeover = token is not None
            await db.execute(
                update(SessionLeaseRow)
                .where(col(SessionLeaseRow.session_id) == lease.session_id)
                .values(lock_token=lease._token, heartbeat_at=func.clock_timestamp())
            )
        await db.commit()
        return takeover
    finally:
        await db.close()


async def _abort_acquisition(task: asyncio.Task[bool]) -> None:
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
            await anyio.sleep(interval)
            db = factory()
            try:
                await _update_owned(db, lease, release=False)
                await db.commit()
            finally:
                # Keep rollback/close in this child before the task group joins it.
                with anyio.fail_after(5, shield=True):
                    await db.close()
    except Exception as error:
        lease._remember(error)
        raise


async def _cleanup(
    lease: SessionLease,
    factory: async_sessionmaker[AsyncSession],
    *,
    release_required: bool,
) -> None:
    # Cleanup alone is bounded; callers finish their work and resources before
    # exiting the context. It is safe to run this DB-only cleanup in another task.
    async with asyncio.timeout(5):
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
    takeover_grace_period: float = 30.0,
) -> AsyncIterator[SessionLease]:
    """Acquire ownership, bind heartbeat to business, and release after all cleanup.

    Enter/exit in the same task: the task group's cancel scope encloses business
    and SDK scopes. Join business children and finish resource cleanup before exit.
    Heartbeat failure cancels business; business exit stops and joins heartbeat.
    This boundary unwraps only the task group's single-error wrapper; concurrent
    real failures remain an exception group. DB release failures retain the original
    error with a diagnostic note. Direct asyncio cancellation is also propagated.

    All competitors use the same finite 0 < heartbeat_interval < heartbeat_timeout
    policy and positive takeover_grace_period. A live owner raises SessionBusy
    without polling. An expired-token takeover renews during its grace period and
    checks ownership before yielding. Grace offers no guarantee that an old process
    exited; each protected transaction still requires lock_owned. Ordinary free
    acquisition does not wait. Cancellation during grace conditionally releases.

    Acquisition cancellation joins the actual transaction before conditionally
    releasing a possibly committed token. DB-only abort/release cleanup is protected
    from caller cancellation with a five-second driver deadline. The engine/pool
    belongs to the application; external effects are not undone or exactly-once.
    """
    if not (
        math.isfinite(heartbeat_interval)
        and math.isfinite(heartbeat_timeout)
        and 0 < heartbeat_interval < heartbeat_timeout
    ):
        raise ValueError("Require finite 0 < heartbeat_interval < heartbeat_timeout")
    if not math.isfinite(takeover_grace_period) or takeover_grace_period <= 0:
        raise ValueError("takeover_grace_period must be finite and positive")
    lease = SessionLease(session_id, uuid4())
    acquired = False
    acquisition = None
    error: BaseException | None = None
    try:
        # Cancellation must stop lock waits while still joining the transaction
        # before release. _acquire uses no context that detaches commit or close.
        acquisition = asyncio.create_task(_acquire(session_factory, lease, heartbeat_timeout))
        try:
            takeover = await asyncio.shield(acquisition)
        except asyncio.CancelledError as cancelled:
            abort = asyncio.create_task(_abort_acquisition(acquisition))
            await _join_protected(abort)
            try:
                abort.result()
            except BaseException as abort_error:
                cancelled.add_note(f"Lease acquisition abort failed: {abort_error!r}")
            raise
        acquired = True
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(
                    _heartbeat,
                    lease,
                    session_factory,
                    heartbeat_interval,
                    name=f"session-heartbeat:{session_id}",
                )
                try:
                    if takeover:
                        await anyio.sleep(takeover_grace_period)
                        async with session_factory.begin() as db:
                            await lease.lock_owned(db)
                    yield lease
                    lease.check()
                finally:
                    group.cancel_scope.cancel()
        except BaseExceptionGroup as grouped:
            if len(grouped.exceptions) == 1:
                # Preserve the original exception's own cause/context.
                raise grouped.exceptions[0]  # noqa: B904
            raise
    except BaseException as caught:
        error = caught
    finally:
        lease._active = False
        if acquisition is not None:
            # A cancelled/failed COMMIT can have an uncertain server outcome.
            # Only after its task has ended may we release this token; a missing
            # token is expected when acquisition rolled back or never succeeded.
            cleanup = asyncio.create_task(
                _cleanup(lease, session_factory, release_required=acquired)
            )
            cancellation = await _join_protected(cleanup)
            if error is None:
                error = cancellation
            try:
                cleanup.result()
            except BaseException as cleanup_error:
                if error is None:
                    error = cleanup_error
                else:
                    error.add_note(f"Lease release failed: {cleanup_error!r}")
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
