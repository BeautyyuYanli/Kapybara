"""Shared session/binding progress; closed confirms registered-resource cleanup only."""

from enum import StrEnum


class LifecycleStatus(StrEnum):
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"


class LifecycleError(RuntimeError):
    """An operation's context expired or its lifecycle no longer permits access."""
