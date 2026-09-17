"""Transport-independent types shared by the execution and control planes."""

from collections.abc import Awaitable, Callable
from typing import Protocol

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type JsonParams = JsonObject | list[JsonValue]
type SendText = Callable[[str], Awaitable[None]]
type ReceiveText = Callable[[], Awaitable[str | None]]
type CloseTransport = Callable[[], Awaitable[None]]
type RequestHandler = Callable[[str, JsonParams], Awaitable[JsonValue]]


class RpcError(Exception):
    """A JSON-RPC error that may safely be returned to the remote caller."""

    def __init__(self, code: int, message: str, data: JsonValue = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RpcDisconnected(Exception):
    """The connection closed; an in-flight operation may still have taken effect."""


class RpcTimeout(Exception):
    """The call deadline expired; this does not cancel a remote side effect."""


class MachineCaller(Protocol):
    """Gateway's authenticated machine routing interface.

    Every machine request includes its target session_id in params. Gateway
    checks that association independently of the caller's session identity.
    """

    async def call(
        self,
        machine_id: str,
        method: str,
        params: JsonObject,
        *,
        timeout: float = 60.0,  # noqa: ASYNC109 - shared machine-caller contract
    ) -> JsonValue: ...
