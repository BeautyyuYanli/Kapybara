"""Run-isolated RPC middleware with one stable SDK durable operation."""

from collections.abc import Callable, Coroutine, Sequence
from copy import copy
from typing import Any

from pydantic import JsonValue, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    CombinedCapability,
    Hooks,
    durable_operation,
)
from pydantic_ai.capabilities.abstract import AgentInstructions
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import AgentStreamEvent, ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.output import OutputContext
from pydantic_ai.tools import ToolDefinition

from .contracts import (
    HookSpec,
    InvokeReply,
    PluginHostError,
    PluginManifest,
    RpcInstructionSpec,
    ValueReply,
)
from .hooks import rpc_hooks
from .rpc import Binding, RpcClient, describe, invoke, json_value


def _instruction(
    binding: Binding, spec: RpcInstructionSpec
) -> Callable[[RunContext[Any]], Coroutine[Any, Any, str | None]]:
    async def instruction(ctx: RunContext[Any]) -> str | None:
        reply = await binding.invoke(ctx, spec.handler, {})
        if not isinstance(reply, ValueReply) or (
            reply.value is not None and not isinstance(reply.value, str)
        ):
            raise binding.data_error("instructions require a string or null value", spec.handler)
        return reply.value

    return instruction


class _Contribution(AbstractCapability[Any]):
    """Fixed instruction callbacks bound to a run Host, without retaining ctx."""

    def __init__(self, binding: Binding, manifest: PluginManifest) -> None:
        self.id = manifest.plugin_id
        self._instructions = [
            _instruction(binding, spec) if isinstance(spec, RpcInstructionSpec) else spec.text
            for spec in manifest.instructions
        ]

    def get_instructions(self) -> AgentInstructions[Any] | None:
        return self._instructions

    @classmethod
    def get_serialization_name(cls) -> None:
        return None


class PluginHostCapability[DepsT](AbstractCapability[DepsT]):
    """An opaque native hook chain resolved independently for each run.

    Register this stable Host directly on the Agent. ``manifests`` synchronously
    reads the run's serialized deps; it must not perform I/O or depend on prompt
    or the live capability registry, which are absent during Activity recovery.
    ``for_run`` copies and validates that snapshot into a same-ID instance. The
    source Host never retains a run context, selection, or intermediate results.

    Only ``invoke_plugin`` resolves borrowed clients. Instructions and hooks call
    this one registered operation, so a durability engine can recover the run
    Host and execute RPC in its Activity. Internal Hooks remain invisible to the
    outer registry and use native ordering independently from the Host's ordering.
    """

    def __init__(
        self,
        manifests: Callable[[RunContext[DepsT]], Sequence[PluginManifest]],
        *,
        client_factory: Callable[[str], RpcClient],
        id: str = "plugin_host",
        ordering: CapabilityOrdering | None = None,
    ) -> None:
        if not id:
            raise ValueError("PluginHostCapability requires a nonempty stable id")
        self.id = id
        self._manifests = manifests
        self._client_factory = client_factory
        self._ordering = ordering
        self._combined = CombinedCapability[Any]([])
        self._exports: set[tuple[str, str, str | None]] = set()

    async def for_run(self, ctx: RunContext[DepsT]) -> PluginHostCapability[DepsT]:
        """Rebuild from deps in both normal runs and SDK Activity recovery."""
        assert self.id is not None
        snapshots = _snapshots(self._manifests(ctx))
        # SDK instruction contexts may lack the run dispatch map. Shallow-copy
        # fixed Agent bindings so durable_operation's normal fallback still works;
        # assembly replaces every selection-dependent field and callback below.
        host = copy(self)
        host._assemble(snapshots)
        return host

    def _assemble(self, manifests: Sequence[PluginManifest]) -> None:
        self._exports = set()
        capabilities: list[AbstractCapability[Any]] = []
        hooks: dict[str, tuple[Hooks[Any], CapabilityOrdering, str, HookSpec]] = {}
        for manifest in manifests:
            plugin_id = manifest.plugin_id
            binding = Binding(plugin_id, self)
            capabilities.append(_Contribution(binding, manifest))
            self._exports.update(
                (plugin_id, spec.handler, None)
                for spec in manifest.instructions
                if isinstance(spec, RpcInstructionSpec)
            )
            for spec in manifest.hooks:
                hook_id = f"{plugin_id}.{spec.id}"
                if hook_id in hooks:
                    raise PluginHostError(
                        f"duplicate hook id {spec.id!r}",
                        phase="declaration",
                        plugin_id=plugin_id,
                        handler=spec.handler,
                        point=spec.point,
                    )
                self._exports.add((plugin_id, spec.handler, spec.point))
                native_ordering = CapabilityOrdering(
                    position=spec.ordering.position if spec.ordering else None
                )
                hook = rpc_hooks(binding, spec, native_ordering)
                hooks[hook_id] = (hook, native_ordering, plugin_id, spec)
                capabilities.append(hook)
        for _, native_ordering, plugin_id, spec in hooks.values():
            if spec.ordering is None:
                continue
            try:
                native_ordering.wraps = [hooks[key][0] for key in spec.ordering.wraps]
                native_ordering.wrapped_by = [hooks[key][0] for key in spec.ordering.wrapped_by]
            except KeyError as error:
                raise PluginHostError(
                    f"unknown hook reference {error.args[0]!r}",
                    phase="declaration",
                    plugin_id=plugin_id,
                    handler=spec.handler,
                    point=spec.point,
                ) from error
        self._combined = CombinedCapability(capabilities)

    @durable_operation(name="invoke_plugin")
    async def invoke_plugin(
        self,
        ctx: RunContext[DepsT],
        plugin_id: str,
        handler: str,
        payload: dict[str, JsonValue],
        *,
        point: str | None = None,
    ) -> InvokeReply:
        """Execute a declared export; ctx is only for SDK durable dispatch.

        Client lookup occurs here, never during assembly or Workflow adaptation.
        Local failures retain their cause; durable failures and retries use the
        engine's native semantics. No ctx or other local resource enters RPC.
        """
        if (plugin_id, handler, point) not in self._exports:
            raise PluginHostError(
                "export is not declared in this run's manifest snapshot",
                phase="declaration",
                plugin_id=plugin_id,
                handler=handler,
                point=point,
            )
        return await invoke(
            self._client_factory(plugin_id), plugin_id, handler, payload, point=point
        )

    def get_ordering(self) -> CapabilityOrdering | None:
        return self._ordering

    def get_instructions(self) -> AgentInstructions[Any] | None:
        return self._combined.get_instructions()

    @property
    def has_on_event(self) -> bool:
        # Merely forwarding on_event must not force every model run to stream.
        return self._combined.has_on_event

    def listens_to(self, event: AgentStreamEvent) -> bool:
        return self._combined.listens_to(event)

    @classmethod
    def get_serialization_name(cls) -> None:
        return None

    async def before_run(self, ctx: RunContext[Any]) -> None:
        await self._combined.before_run(ctx)

    async def prepare_tools(
        self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return await self._combined.prepare_tools(ctx, tool_defs)

    async def prepare_output_tools(
        self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return await self._combined.prepare_output_tools(ctx, tool_defs)

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        return await self._combined.before_model_request(ctx, request_context)

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        return await self._combined.after_model_request(
            ctx, request_context=request_context, response=response
        )

    async def on_model_request_error(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        return await self._combined.on_model_request_error(
            ctx, request_context=request_context, error=error
        )

    async def before_tool_validate(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: str | dict[str, Any],
    ) -> str | dict[str, Any]:
        return await self._combined.before_tool_validate(
            ctx, call=call, tool_def=tool_def, args=args
        )

    async def after_tool_validate(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._combined.after_tool_validate(
            ctx, call=call, tool_def=tool_def, args=args
        )

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._combined.before_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args
        )

    async def after_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        return await self._combined.after_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, result=result
        )

    async def on_tool_validate_error(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: str | dict[str, Any],
        error: ValidationError | ModelRetry,
    ) -> dict[str, Any]:
        return await self._combined.on_tool_validate_error(
            ctx, call=call, tool_def=tool_def, args=args, error=error
        )

    async def on_tool_execute_error(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        error: Exception,
    ) -> Any:
        return await self._combined.on_tool_execute_error(
            ctx, call=call, tool_def=tool_def, args=args, error=error
        )

    async def before_output_validate(
        self, ctx: RunContext[Any], *, output_context: OutputContext, output: str | dict[str, Any]
    ) -> str | dict[str, Any]:
        return await self._combined.before_output_validate(
            ctx, output_context=output_context, output=output
        )

    async def on_event(self, ctx: RunContext[Any], *, event: AgentStreamEvent) -> None:
        await self._combined.on_event(ctx, event=event)


async def describe_plugins(clients: Sequence[RpcClient]) -> list[PluginManifest]:
    """Discover serializable declarations in client order, rejecting duplicate IDs.

    Calls only ``plugin.describe`` and returns data, without clients or SDK
    objects. The caller can serialize these manifests into run dependencies.
    Clients are borrowed; RPC errors and cancellation retain the shared boundary.
    """
    manifests: list[PluginManifest] = []
    plugins: set[str] = set()
    for client in tuple(clients):
        manifest = await describe(client)
        if manifest.plugin_id in plugins:
            raise PluginHostError(
                f"duplicate plugin identity {manifest.plugin_id!r}",
                phase="declaration",
                plugin_id=manifest.plugin_id,
            )
        plugins.add(manifest.plugin_id)
        manifests.append(manifest)
    return manifests


def _snapshots(manifests: Sequence[PluginManifest]) -> list[PluginManifest]:
    """Revalidate and copy JSON declarations before exposing any bound callbacks."""
    snapshots: list[PluginManifest] = []
    plugins: set[str] = set()
    for declaration in manifests:
        try:
            manifest = PluginManifest.model_validate(
                json_value(declaration.model_dump(warnings="error"))
            )
        except ValueError as error:
            raise PluginHostError(
                "invalid plugin manifest", phase="declaration", plugin_id=declaration.plugin_id
            ) from error
        if manifest.plugin_id in plugins:
            raise PluginHostError(
                f"duplicate plugin identity {manifest.plugin_id!r}",
                phase="declaration",
                plugin_id=manifest.plugin_id,
            )
        plugins.add(manifest.plugin_id)
        snapshots.append(manifest)
    return snapshots


def build_plugin_host(
    manifests: Sequence[PluginManifest],
    *,
    client_factory: Callable[[str], RpcClient],
    id: str = "plugin_host",
    ordering: CapabilityOrdering | None = None,
) -> PluginHostCapability[Any]:
    """Build a validated static snapshot without looking up clients or doing RPC.

    Later caller mutations cannot affect this Host. Each run still rebuilds an
    independent same-ID instance; client lookup is delayed until an invocation.
    Invalid declarations return no partial Host; sorting retains SDK errors.
    """
    snapshots = _snapshots(manifests)
    host = PluginHostCapability[Any](
        lambda ctx: snapshots, client_factory=client_factory, id=id, ordering=ordering
    )
    host._assemble(snapshots)
    return host


async def load_plugin_host(
    clients: Sequence[RpcClient],
    *,
    id: str = "plugin_host",
    ordering: CapabilityOrdering | None = None,
) -> PluginHostCapability[Any]:
    """Discover and build while retaining each declaration's original client.

    This convenience entry point performs discovery RPC. In a durable Workflow,
    discover in a caller-owned Activity and supply that snapshot through deps to
    a Host registered on the Agent instead. No entry point closes borrowed clients
    or returns a partial Host.
    """
    bound_clients = tuple(clients)
    manifests = await describe_plugins(bound_clients)
    clients_by_plugin = {
        manifest.plugin_id: client
        for manifest, client in zip(manifests, bound_clients, strict=True)
    }
    return build_plugin_host(
        manifests, client_factory=clients_by_plugin.__getitem__, id=id, ordering=ordering
    )
