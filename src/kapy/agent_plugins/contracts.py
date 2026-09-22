"""Builtin plugin boundary: identity, copied configuration and scoped JSON state only.

This is a responsibility boundary, not a sandbox for untrusted Python. Plugins
own external clients/tasks and resource semantics; no SDK or database object is
part of their context. Instructions are read-only and may be evaluated repeatedly.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue
from pydantic_ai.capabilities import AbstractCapability

from kapy.control.types import DTO
from kapy.lifecycle import LifecycleStatus

type PluginName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]


@dataclass(frozen=True)
class VersionedState[StateT: BaseModel]:
    """An independent state value and an opaque UUID concurrency token.

    None represents absent/initial state. Mutating value does not persist it;
    call StateStore.replace explicitly. Revisions support equality checks only,
    not ordering, change counts or access to historical snapshots.
    """

    revision: UUID
    value: StateT | None


class StateStore[StateT: BaseModel](Protocol):
    """Host-scoped access to one binding's registered state type and data version.

    Each call owns a separate short DB transaction, never a transaction spanning
    plugin code or external I/O. State and runner history/checkpoint commit
    separately; neither makes an external effect atomic with its tool result.
    Only business state is writable, not binding identity, config or lifecycle.

    The host admits execution only while ready and close only after deciding
    closing, under the same session lease. Each state transaction first fences
    ownership, then accesses the binding; a lost owner cannot read or write state.
    The host revokes the store after plugin cleanup. LifecycleError reports expired
    access or instruction writes; LeaseLost reports lost ownership. Neither is a
    StateConflict to retry. Cleanup must support locally held resource references
    when state access is unavailable.
    """

    async def read(self) -> VersionedState[StateT]:
        """Read and validate the current state, returning an independent value.

        This is not a resource reservation or a lock across subsequent plugin
        work: another tool may write before replace, or ownership may be lost.
        Reading and changing the returned object do not advance the revision.
        """
        ...

    async def replace(self, value: StateT, *, expected_revision: UUID) -> VersionedState[StateT]:
        """Replace the whole state using a revision obtained from read/replace.

        Value must use the registered StateT and round-trip through JSON; custom
        validators/serializers must not perform I/O or mutate external state.
        Validation happens outside the transaction. A successful CAS atomically
        saves state and a new UUID, returning their independent typed value.
        A stale revision raises StateConflict without overwriting newer data;
        reread and decide how to merge, preserving all resource cleanup references.
        The host never retries or merges plugin mutations automatically.

        Instructions cannot call replace: it raises LifecycleError. Lifecycle
        rejection also leaves state/revision unchanged. External effects already
        performed are not rolled back; an uncertain DB commit requires checking
        ownership/progress, not blindly deleting a possibly registered resource.
        """
        ...


@dataclass(frozen=True)
class SessionContext[ConfigT: BaseModel, StateT: BaseModel]:
    """One execution or close operation's identity, copied config and scoped state.

    The host validates config/state and commits any required migration before
    constructing this object. Config is an independent current-format value;
    changing it does not update stored configuration. Read current state through
    the store rather than assuming the object contains a fixed state snapshot.

    Both plugin methods receive this type, but each operation gets its own store
    lifetime. Identity fields identify resource ownership, not access
    credentials. No DB connection, SDK RunContext, logger or resource backend is
    injected; the plugin owns the clients it creates within the operation.
    """

    session_id: UUID
    plugin_provider: str
    plugin_name: str
    config: ConfigT
    state: StateStore[StateT]


@dataclass(frozen=True)
class PluginTool:
    """A typed business callable using a captured SessionContext.

    The host supplies no SDK RunContext argument. Native SDK dispatch supports
    sync, async and sync-returning-awaitable functions; parameters keep their
    declared types and names. Name is local, nonempty ASCII letters/digits/_;
    the host adds provider/plugin prefixes and rejects collisions or final names
    longer than 64 characters.

    Tools may execute concurrently and replay after runner recovery. The plugin
    must handle state conflicts and design any required business idempotency;
    a state commit is not atomic with the external effect or saved tool result.
    """

    name: str
    description: str
    function: Callable[..., Any]


@dataclass(frozen=True)
class PluginBinding:
    """Contributions valid within the yielding open_execution context.

    Callables may capture the context and resources owned by that execution;
    they must not escape its lifetime. Instructions may be evaluated repeatedly,
    including during recovery and compaction. They only render text/read state:
    no allocation, state writes or external mutations. The host rejects state
    writes while evaluating instructions and restores tool access afterwards.

    Native capabilities are trusted SDK extensions: their hooks/instructions/tools
    do not receive the adapter's scope checks, read-only guard or tool prefixes.
    They own valid final tool names and may use captured resources only within
    this execution. Use for_run() for state local to each SDK run, including
    auxiliary paging runs. Plugins contribute capabilities; only the host installs
    them, and the host supplies the session checkpoint ordering boundary.
    """

    instructions: Callable[[], Awaitable[str]] | None = None
    tools: tuple[PluginTool, ...] = ()
    capabilities: tuple[AbstractCapability[Any], ...] = ()


@dataclass(frozen=True)
class PluginData:
    """Raw config/state JSON passed to a plugin's independent data migrations.

    Config may be JSON null; state may be absent or describe partial resource
    work. A migration n -> n+1 is a pure synchronous function returning new data
    without mutating the input. It receives no context/store and performs no I/O
    or resource operations. Preserve ownership and pending cleanup references;
    changing JSON format must not recreate resources or rewrite model history.
    """

    config: JsonValue
    state: JsonValue


class AgentPlugin[ConfigT: BaseModel, StateT: BaseModel](ABC):
    """A reconstructible, no-argument instance for one execution or close operation.

    Construction only initializes memory: no I/O, tasks or custom injected
    arguments. The host creates fresh Python objects for execution and close;
    resource ownership is the stable (session_id, plugin_provider, plugin_name),
    not object identity. Use persisted state to reuse resources across executions
    and to close them from a new instance. ConfigT/StateT must round-trip through
    JSON; their validators and serializers must be free of I/O/external mutations.

    Long-lived resources may be allocated lazily in open_execution/tools. Read
    state before allocation, promptly register references and retain all progress
    needed for cleanup. Parallel first use and retry/replay are plugin concerns:
    UUID CAS prevents lost state updates, not duplicate external creation.

    Execution and closing share one exclusive session lease. A lost owner may
    still have in-flight external work; a takeover grace period does not prove exit.
    On denied registration or cancellation, stop further allocation and use local
    references for bounded best-effort cleanup of known unregistered resources.
    Do not blindly delete resources whose registration may have committed. Tag
    resources with discoverable session/provider/name ownership and provide
    reconciliation or reliable expiry for late/ambiguous orphaned allocations.
    The host only confirms registered cleanup; it cannot interpret arbitrary state
    or guarantee that every external request or orphan is gone when closed.
    """

    @abstractmethod
    def open_execution(
        self, ctx: SessionContext[ConfigT, StateT]
    ) -> AbstractAsyncContextManager[PluginBinding]:
        """Yield one PluginBinding while owning this execution's local resources.

        Accept validated config and initially absent state; there is no separate
        session initialization callback. Allocate/reuse resources here or in tools
        as needed. The host enters/exits the context in the runner's owning task
        and closes the SDK graph before exit. Clients and child tasks must stay
        within this scope and be closed/joined before exit completes.

        If setup fails before yielding, clean everything locally acquired that
        will not be retained as registered session resources. On normal exit,
        retain those registered resources for later executions or close_session.
        Cleanup must tolerate deleted resources and revoked StateStore access.
        On cancellation/error, perform bounded cleanup and propagate the failure,
        preserving cleanup errors too; do not suppress a failed execution or leave
        detached work. StateStore is revoked once this context exits.
        """
        ...

    async def close_session(self, ctx: SessionContext[ConfigT, StateT]) -> None:
        """Confirm all registered resources deleted/absent before returning.

        Override this no-op whenever the plugin owns resources needing session
        cleanup. Accept absent/partial state, repeated calls and in-flight external
        deletes from a lost owner. A missing resource counts as deleted;
        an accepted asynchronous deletion request alone does not count as done.
        Never allocate replacement session resources or start detached deletes.
        Destroy only session-owned resources; release borrowed shared resources.

        Save useful partial progress through StateStore. Close callbacks run
        sequentially under the lease; ownership loss rejects further persistence.
        Errors/cancellation propagate after bounded local cleanup; the host retains
        committed progress for an explicit retry. Never hide cleanup failures.

        Return only after registered cleanup and local client/task cleanup finish.
        The host then conditionally records closed; plugins cannot advance that
        status themselves. The lease covers execution cleanup, but cannot promise
        every lost-owner request or late/unregistered orphan is gone. The store
        expires on method exit.
        """
        return None


class PluginSpec(DTO):
    plugin_provider: PluginName
    plugin_name: PluginName
    config: JsonValue


class BindingRecord(DTO):
    session_id: UUID
    plugin_provider: str
    plugin_name: str
    data_version: int
    config: JsonValue
    state: JsonValue
    revision: UUID
    status: LifecycleStatus
    created_at: datetime
    updated_at: datetime


class StateConflict(RuntimeError):
    """UUID revision differs; reread state and decide how to merge, never blind retry."""


class PluginOperationError(RuntimeError):
    """A plugin operation failed; the cause retains its original diagnostic."""

    def __init__(self, provider: str, name: str, operation: str) -> None:
        self.plugin_provider, self.plugin_name = provider, name
        super().__init__(f"Plugin {provider}.{name}: {operation} failed")
