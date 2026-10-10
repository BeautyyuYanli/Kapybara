"""Shared progress; creating is Session-only, while legacy bindings start ready.

Closed confirms configured cleanup, not discovery/revocation of external requests.
"""

from enum import StrEnum


class LifecycleStatus(StrEnum):
    CREATING = "creating"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"


class LifecycleError(RuntimeError):
    """An operation's context expired or its lifecycle no longer permits access."""
