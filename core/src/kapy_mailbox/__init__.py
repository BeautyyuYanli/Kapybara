"""Redis-backed agent mailbox primitives.

This package implements the proposal in
`.context/proposals/260515-1659-agent-mailbox-design.md` as a lightweight,
Redis-client-compatible mailbox layer. `RedisMailbox` owns no Redis lifecycle or
background tasks; producer supervision and automatic compaction are separate
objects.
"""

from kapy_mailbox.exceptions import (
    AutoCompactorClosedError,
    InvalidChannelError,
    InvalidMessageFilterError,
    InvalidMessageWindowError,
    NamespaceNotAllowedError,
    PayloadSerializationError,
    ProducerAlreadyRegisteredError,
    ProducerSupervisorClosedError,
    UnknownMessageError,
)
from kapy_mailbox.models import (
    CancellationToken,
    CompactionResult,
    ConsumeResult,
    Message,
    MessageFilter,
    MessageInput,
    ProducerHandle,
    ProducerStatus,
    RestartPolicy,
    RetentionPolicy,
)
from kapy_mailbox.redis import (
    MailboxAutoCompactor,
    MailboxProducerSupervisor,
    MailboxWriter,
    RedisMailbox,
)
from kapy_mailbox.serialization import JSONMessageSerializer, MessageSerializer

__all__ = [
    "AutoCompactorClosedError",
    "CancellationToken",
    "CompactionResult",
    "ConsumeResult",
    "InvalidChannelError",
    "InvalidMessageFilterError",
    "InvalidMessageWindowError",
    "JSONMessageSerializer",
    "MailboxAutoCompactor",
    "MailboxProducerSupervisor",
    "MailboxWriter",
    "Message",
    "MessageFilter",
    "MessageInput",
    "MessageSerializer",
    "NamespaceNotAllowedError",
    "PayloadSerializationError",
    "ProducerAlreadyRegisteredError",
    "ProducerHandle",
    "ProducerStatus",
    "ProducerSupervisorClosedError",
    "RedisMailbox",
    "RestartPolicy",
    "RetentionPolicy",
    "UnknownMessageError",
]
