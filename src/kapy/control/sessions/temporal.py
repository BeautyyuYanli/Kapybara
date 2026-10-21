"""Native session Activity boundaries and safe public failure categories.

These values are not persisted by Kapy. Temporal input is read solely to check
session/type ownership; prompt, configuration and exception text stay private.
"""

from pydantic_ai.exceptions import UserError
from temporalio.client import ActivityExecutionStatus
from temporalio.exceptions import ApplicationError, CancelledError, TerminatedError, TimeoutError

from kapy.lifecycle import LifecycleError

from .types import OwnershipLost, SessionAlreadyExists, SessionBusy

CREATE = "kapy.create_session"
RUN = "kapy.run_agent"
RESUME = "kapy.resume_agent"
CLOSE = "kapy.close_session"
TERMINAL_STATUSES = frozenset(
    {
        ActivityExecutionStatus.COMPLETED,
        ActivityExecutionStatus.FAILED,
        ActivityExecutionStatus.CANCELED,
        ActivityExecutionStatus.TERMINATED,
        ActivityExecutionStatus.TIMED_OUT,
    }
)
_BUSINESS_ERRORS = (SessionBusy, LifecycleError, SessionAlreadyExists, OwnershipLost)
_SAFE_CATEGORIES = {
    "SessionBusy",
    "LifecycleError",
    "SessionAlreadyExists",
    "OwnershipLost",
    "NotFound",
    "InvalidParameters",
    "ExecutionFailed",
    "Canceled",
    "TimedOut",
    "Terminated",
    "UnableToConfirm",
}


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
