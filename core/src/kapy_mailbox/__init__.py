"""PostgreSQL-backed agent mailbox primitives.

This package implements the proposal in
`.context/proposals/260521-0207-split-mailbox-read-write.md` as a split mailbox
layer with separate writer, inbox, and maintenance APIs. Mailbox components own
no database lifecycle; producer supervision is separate, and automatic
compaction is exposed through explicit context-managed or blocking entry points.
`SQLModelPostgresMailboxStorage` is the bundled production adapter for
SQLModel-backed PostgreSQL persistence over psycopg's async driver.
"""

from kapy_mailbox.exceptions import (
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
from kapy_mailbox.postgres import (
    MailboxProducerSupervisor,
    NamespaceRetentionProvider,
    PostgresMailboxInbox,
    PostgresMailboxMaintenance,
    PostgresMailboxStorage,
    PostgresMailboxWriter,
    SQLModelPostgresMailboxStorage,
)
from kapy_mailbox.serialization import JSONMessageSerializer, MessageSerializer

__all__ = [
    "CancellationToken",
    "CompactionResult",
    "ConsumeResult",
    "InvalidChannelError",
    "InvalidMessageFilterError",
    "InvalidMessageWindowError",
    "JSONMessageSerializer",
    "MailboxProducerSupervisor",
    "Message",
    "MessageFilter",
    "MessageInput",
    "MessageSerializer",
    "NamespaceNotAllowedError",
    "NamespaceRetentionProvider",
    "PayloadSerializationError",
    "PostgresMailboxInbox",
    "PostgresMailboxMaintenance",
    "PostgresMailboxStorage",
    "PostgresMailboxWriter",
    "ProducerAlreadyRegisteredError",
    "ProducerHandle",
    "ProducerStatus",
    "ProducerSupervisorClosedError",
    "RestartPolicy",
    "RetentionPolicy",
    "SQLModelPostgresMailboxStorage",
    "UnknownMessageError",
]
