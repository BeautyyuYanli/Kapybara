"""Domain exceptions for the Redis-backed mailbox."""

from __future__ import annotations


class MailboxError(Exception):
    """Base class for mailbox-specific failures."""


class InvalidChannelError(MailboxError):
    """Raised when a message channel violates the shared channel path rules."""


class PayloadSerializationError(MailboxError):
    """Raised when the configured serializer cannot encode a payload."""


class InvalidMessageFilterError(MailboxError):
    """Raised when a filter contains contradictory or malformed predicates."""


class InvalidMessageWindowError(MailboxError):
    """Raised when mailbox query window parameters are invalid."""


class UnknownMessageError(MailboxError):
    """Raised by strict consume when one or more message ids are unknown."""

    def __init__(self, message_ids: list[str]) -> None:
        self.message_ids = list(message_ids)
        super().__init__(f"Unknown message ids: {', '.join(self.message_ids)}")


class ProducerAlreadyRegisteredError(MailboxError):
    """Raised when the same producer name is registered twice."""


class ProducerSupervisorClosedError(MailboxError):
    """Raised when a stopped supervisor is used again."""


class AutoCompactorClosedError(MailboxError):
    """Raised when a stopped auto-compactor is used again."""
