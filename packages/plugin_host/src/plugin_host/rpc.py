"""Borrowed RPC clients and one strict boundary for all plugin calls."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_ai import RunContext

from .contracts import InvokeReply, LocalId, PluginHostError, PluginManifest

if TYPE_CHECKING:
    from .capability import PluginHostCapability

_JSON = TypeAdapter(JsonValue, config=ConfigDict(strict=True, allow_inf_nan=False))
_REPLY = TypeAdapter(InvokeReply)
_PLUGIN_ID = TypeAdapter(LocalId, config=ConfigDict(strict=True))


class RpcClient(Protocol):
    """Caller-owned asynchronous client, supporting concurrent calls.

    Implementations own transport, timeouts, and remote execution. Cancellation
    must propagate; cancelling a local await does not promise remote rollback.
    The Host never closes this client and never retries an invocation.
    """

    async def call(self, method: str, params: dict[str, JsonValue]) -> JsonValue: ...


def json_value(value: Any) -> JsonValue:
    """Validate JSON-native data without converting objects or nonfinite numbers."""
    return _JSON.validate_python(value)


async def describe(client: RpcClient) -> PluginManifest:
    try:
        data = await client.call("plugin.describe", {})
    except Exception as error:
        raise PluginHostError("plugin.describe failed", phase="rpc") from error
    plugin_id = None
    if isinstance(data, dict):
        try:
            plugin_id = _PLUGIN_ID.validate_python(data.get("plugin_id"))
        except ValidationError:
            pass
    try:
        return PluginManifest.model_validate(json_value(data))
    except ValidationError as error:
        raise PluginHostError(
            "invalid plugin manifest", phase="declaration", plugin_id=plugin_id
        ) from error


@dataclass(frozen=True)
class Binding:
    """A fixed identity and run Host operation; it retains no client or context."""

    plugin_id: str
    host: PluginHostCapability[Any]

    def data_error(self, message: str, handler: str, point: str | None = None) -> PluginHostError:
        return PluginHostError(
            message, phase="data", plugin_id=self.plugin_id, handler=handler, point=point
        )

    async def invoke(
        self,
        ctx: RunContext[Any],
        handler: str,
        payload: dict[str, Any],
        *,
        point: str | None = None,
    ) -> InvokeReply:
        # Reject non-JSON local input before a durable schema can coerce it.
        try:
            data = cast(dict[str, JsonValue], json_value(payload))
        except ValidationError as error:
            raise self.data_error("input is not JSON-native data", handler, point) from error
        return await self.host.invoke_plugin(ctx, self.plugin_id, handler, data, point=point)


async def invoke(
    client: RpcClient,
    plugin_id: str,
    handler: str,
    payload: dict[str, JsonValue],
    *,
    point: str | None = None,
) -> InvokeReply:
    """Invoke and validate at the execution boundary, without passing local ctx."""
    try:
        params = {"handler": handler, "payload": json_value(payload)}
    except ValidationError as error:
        raise PluginHostError(
            "input is not JSON-native data",
            phase="data",
            plugin_id=plugin_id,
            handler=handler,
            point=point,
        ) from error
    try:
        result = await client.call("plugin.invoke", params)
    except Exception as error:
        raise PluginHostError(
            "plugin.invoke failed",
            phase="rpc",
            plugin_id=plugin_id,
            handler=handler,
            point=point,
        ) from error
    try:
        return _REPLY.validate_python(json_value(result))
    except ValidationError as error:
        raise PluginHostError(
            "invalid invocation reply",
            phase="data",
            plugin_id=plugin_id,
            handler=handler,
            point=point,
        ) from error
