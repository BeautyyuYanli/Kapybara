"""Native session Activity boundaries and safe public failure categories.

These values are not persisted by Kapy. Temporal input is read solely to check
session/type ownership; prompt, configuration and exception text stay private.
"""

from datetime import timedelta
from uuid import UUID

from pydantic_ai.exceptions import UserError
from temporalio.client import ActivityExecutionStatus, Client
from temporalio.exceptions import ApplicationError, CancelledError, TerminatedError, TimeoutError
from temporalio.service import RPCError, RPCStatusCode

from kapy.lifecycle import LifecycleError

from .types import OwnershipLost, SessionBusy

RUN = "kapy.run_agent"
TEMPORAL_RPC_TIMEOUT = timedelta(seconds=10)
TERMINAL_STATUSES = frozenset(
    {
        ActivityExecutionStatus.COMPLETED,
        ActivityExecutionStatus.FAILED,
        ActivityExecutionStatus.CANCELED,
        ActivityExecutionStatus.TERMINATED,
        ActivityExecutionStatus.TIMED_OUT,
    }
)
_BUSINESS_ERRORS = (SessionBusy, LifecycleError, OwnershipLost)
_SAFE_CATEGORIES = {
    "SessionBusy",
    "LifecycleError",
    "OwnershipLost",
    "NotFound",
    "InvalidParameters",
    "ExecutionFailed",
    "Canceled",
    "TimedOut",
    "Terminated",
    "UnableToConfirm",
}


async def ensure_activity_terminal(client: Client, activity_id: UUID, run_id: str) -> None:
    """Confirm the exact observed writer is terminal or absent, outside the database transaction."""
    try:
        description = await client.get_activity_handle(str(activity_id), run_id=run_id).describe(
            rpc_timeout=TEMPORAL_RPC_TIMEOUT
        )
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            raise
    else:
        if description.status not in TERMINAL_STATUSES:
            raise SessionBusy("Previous native execution may still be active")


def failure_category(error: BaseException | None) -> str:
    """Use only known categories, never raw exception messages or details."""
    if isinstance(error, ApplicationError):
        return (
            error.type
            if error.type is not None and error.type in _SAFE_CATEGORIES
            else "ExecutionFailed"
        )
    if isinstance(error, _BUSINESS_ERRORS):
        return type(error).__name__
    if isinstance(error, RPCError):
        return "UnableToConfirm"
    if isinstance(error, LookupError):
        return "NotFound"
    if isinstance(error, (ValueError, TypeError, UserError)):
        return "InvalidParameters"
    if isinstance(error, CancelledError):
        return "Canceled"
    if isinstance(error, TimeoutError):
        return "TimedOut"
    if isinstance(error, TerminatedError):
        return "Terminated"
    return "ExecutionFailed"
