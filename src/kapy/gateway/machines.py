"""Fenced duplex connections and session-machine association gates."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from uuid import UUID
from weakref import WeakValueDictionary

from kapy.rpc import (
    JsonObject,
    JsonParams,
    JsonValue,
    RpcDisconnected,
    RpcError,
    RpcPeer,
    RpcTimeout,
)
from kapy.state import NotFound

from .auth import Authenticator, denied
from .storage import Metadata

if TYPE_CHECKING:
    from kapy.state import SessionService

    from .control import ControlService


def offline() -> RpcError:
    return RpcError(-32022, "Machine is offline", {"kind": "offline", "retryable": True})


@dataclass(eq=False, slots=True)
class Connection:
    machine_id: str
    peer: RpcPeer
    associations: dict[UUID, asyncio.Task[None]] = field(default_factory=dict)


class MachineRegistry:
    def __init__(
        self,
        auth: Authenticator,
        metadata: Metadata,
        sessions: Callable[[], SessionService],
    ) -> None:
        self.auth = auth
        self.metadata = metadata
        self.sessions = sessions
        self.connections: dict[str, Connection] = {}
        self._changed = asyncio.Condition()
        self._session_locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()

    def session_lock(self, session_id: UUID) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def current(self, connection: Connection) -> None:
        if self.connections.get(connection.machine_id) is not connection:
            raise offline()

    async def register(self, connection: Connection) -> None:
        async with self._changed:
            old = self.connections.get(connection.machine_id)
            self.connections[connection.machine_id] = connection
            self._changed.notify_all()
        if old is not None:
            await old.peer.aclose()
        after = None
        while True:
            page = await self.sessions().list_sessions(after=after, limit=200)
            for session in page.items:
                if connection.machine_id in session.machine_ids:
                    # Schedule proactively, while preserving a single future per association.
                    self.ensure_task(connection, session.id)
            after = page.next_after
            if after is None:
                break

    def prepare(self, session_id: UUID, machine_ids: tuple[str, ...]) -> None:
        for machine_id in machine_ids:
            connection = self.connections.get(machine_id)
            if connection is not None:
                self.ensure_task(connection, session_id)

    async def unregister(self, connection: Connection) -> None:
        async with self._changed:
            if self.connections.get(connection.machine_id) is connection:
                del self.connections[connection.machine_id]
                self._changed.notify_all()
        for task in connection.associations.values():
            task.cancel()
        await asyncio.gather(*connection.associations.values(), return_exceptions=True)

    async def _ensure(self, connection: Connection, session_id: UUID) -> None:
        while await self.metadata.access(session_id) is None:
            self.current(connection)
            await asyncio.sleep(0.05)
        async with self.session_lock(session_id):
            session = await self.sessions().get_session(session_id)
            if connection.machine_id not in session.machine_ids:
                raise denied("Machine is not associated with this session")
            cleanup = await self.metadata.rows(
                "SELECT session_id FROM gateway_session_cleanup WHERE session_id=%s",
                (session_id,),
            )
            if cleanup:
                raise denied("Session is being deleted")
            self.current(connection)
            await self.metadata.rows(
                "INSERT INTO gateway_machine_resources VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (session_id, connection.machine_id),
            )
            await connection.peer.call(
                "session.ensure",
                {
                    "session_id": str(session_id),
                    "session_token": self.auth.token(session_id, connection.machine_id),
                },
            )
            self.current(connection)

    def ensure_task(self, connection: Connection, session_id: UUID) -> asyncio.Task[None]:
        task = connection.associations.get(session_id)
        if task is None or task.done() and (task.cancelled() or task.exception() is not None):
            task = asyncio.create_task(self._ensure(connection, session_id))
            connection.associations[session_id] = task
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    async def call(
        self,
        machine_id: str,
        method: str,
        params: JsonObject,
        *,
        timeout: float = 60.0,  # noqa: ASYNC109
    ) -> JsonValue:
        if machine_id not in self.auth.settings.machine_tokens:
            raise RpcError(-32004, "Machine not found", {"kind": "not_found"})
        if not method.startswith(("process.", "file.")):
            raise denied("Machine lifecycle methods are internal")
        try:
            session_id = UUID(cast(str, params.get("session_id")))
        except ValueError, TypeError, AttributeError:
            raise RpcError(-32602, "A target session_id is required") from None
        deadline = time.monotonic() + timeout
        sent = False
        try:
            async with asyncio.timeout(timeout):
                session = await self.sessions().get_session(session_id)
                if machine_id not in session.machine_ids:
                    raise denied("Machine is not associated with this session")
                async with self._changed:
                    await self._changed.wait_for(lambda: machine_id in self.connections)
                    connection = self.connections[machine_id]
                await asyncio.shield(self.ensure_task(connection, session_id))
                # Recheck association and deletion after waiting: updates can revoke access.
                session = await self.sessions().get_session(session_id)
                access = await self.metadata.access(session_id)
                cleanup = await self.metadata.rows(
                    "SELECT session_id FROM gateway_session_cleanup WHERE session_id=%s",
                    (session_id,),
                )
                if (
                    not access
                    or access["deleted"]
                    or cleanup
                    or machine_id not in session.machine_ids
                ):
                    raise denied("Session association has been revoked")
                self.current(connection)
                sent = True
                return await connection.peer.call(
                    method,
                    params,
                    timeout=max(0.001, deadline - time.monotonic()),
                )
        except NotFound:
            raise RpcError(-32004, "Session not found", {"kind": "not_found"}) from None
        except TimeoutError, RpcTimeout:
            if sent:
                raise RpcError(
                    -32022,
                    "Call timed out; operation result is unknown",
                    {
                        "kind": "offline",
                        "retryable": False,
                        "unknown": True,
                    },
                ) from None
            raise offline() from None
        except RpcDisconnected:
            raise RpcError(
                -32022,
                "Connection lost; operation result is unknown",
                {
                    "kind": "offline",
                    "retryable": False,
                    "unknown": True,
                },
            ) from None

    async def proxy(
        self,
        connection: Connection,
        method: str,
        params: JsonParams,
        control: ControlService,
    ) -> JsonValue:
        self.current(connection)
        if method != "control.proxy":
            raise RpcError(-32601, "Method not found")
        if not isinstance(params, dict) or set(params) != {"auth", "method", "params"}:
            raise RpcError(-32602, "Invalid proxy envelope")
        auth = params["auth"]
        target = params["method"]
        arguments = params["params"]
        if (
            not isinstance(auth, dict)
            or not isinstance(target, str)
            or not isinstance(arguments, dict)
        ):
            raise RpcError(-32602, "Invalid proxy envelope")
        if not target.startswith(("session.", "history.", "event.", "skill.")):
            raise denied("Proxy method is not permitted")
        token = auth.get("token")
        if not isinstance(token, str):
            raise denied()
        if auth.get("kind") == "user" and set(auth) == {"kind", "token"}:
            principal = self.auth.operator(token)
        elif auth.get("kind") == "session" and set(auth) == {"kind", "token", "session_id"}:
            try:
                sid = UUID(cast(str, auth["session_id"]))
            except ValueError, TypeError, AttributeError:
                raise denied() from None
            principal = self.auth.session(sid, connection.machine_id, token)
            await asyncio.shield(self.ensure_task(connection, sid))
        else:
            raise denied()
        self.current(connection)
        return await control.call(target, arguments, principal=principal)

    async def release(self, machine_id: str, session_id: UUID) -> bool:
        connection = self.connections.get(machine_id)
        if connection is None:
            return False
        try:
            result = await connection.peer.call(
                "session.release",
                {
                    "session_id": str(session_id),
                    "wait_ms": 5000,
                },
                timeout=10,
            )
        except RpcDisconnected, RpcTimeout:
            return False
        released = isinstance(result, dict) and result.get("released") is True
        if released:
            await self.metadata.rows(
                "DELETE FROM gateway_machine_resources WHERE session_id=%s AND machine_id=%s",
                (session_id, machine_id),
            )
            task = connection.associations.pop(session_id, None)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return released

    async def aclose(self) -> None:
        for connection in tuple(self.connections.values()):
            await connection.peer.aclose()
            await self.unregister(connection)
