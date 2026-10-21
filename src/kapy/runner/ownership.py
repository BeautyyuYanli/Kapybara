"""Run-local fencing at actual SDK callback entries, outside callback I/O.

WrapperCapability and visit_and_replace retain SDK ordering, rebinding and error
recovery. SDK 2.54 exposes neither a root replacement argument nor a visitor for
Hooks registrations/marked event methods. The compatibility points below copy
that root/registry and guard individual dispatch entries, including native output
validators; callback filters, timeouts and sync execution stay with native
dispatch. Nothing here mutates the static Agent, installed definitions or layer
State.
"""

import asyncio
from copy import copy
from dataclasses import replace
from functools import cache, wraps
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent, RunContext
from pydantic_ai._output import OutputValidator
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    CombinedCapability,
    Hooks,
    WrapModelRequestHandler,
    WrapNodeRunHandler,
    WrapOutputProcessHandler,
    WrapOutputValidateHandler,
    WrapperCapability,
    WrapRunHandler,
    WrapToolExecuteHandler,
    WrapToolValidateHandler,
    on_event,
)
from pydantic_ai.capabilities._on_event import collect_on_event_methods
from pydantic_ai.capabilities.hooks import _call_func
from temporalio import activity

from kapy.lifecycle import LifecycleStatus
from pydantic_ai_layer_protocol import LayerRegistry

from .recording import MessageRecordCapability
from .scopes import check_owner

if TYPE_CHECKING:
    from .types import RunnerDeps


def _check_running(ctx: RunContext[RunnerDeps]) -> None:
    ctx.deps.recorder.ensure_running()
    task = asyncio.current_task()
    if (task is not None and task.cancelling()) or (
        activity.in_activity() and activity.is_cancelled()
    ):
        raise asyncio.CancelledError


async def check(ctx: RunContext[RunnerDeps]) -> None:
    """Reject further callbacks/work after a failed gate, save or real cancellation."""
    try:
        _check_running(ctx)
        await check_owner(
            ctx.deps.session_factory,
            ctx.deps.session_id,
            ctx.deps.activity_id,
            ctx.deps.activity_run_id,
            (LifecycleStatus.READY,),
        )
        # Parallel SDK callbacks can stop/cancel the execution while this short
        # database check waits. Recheck local facts immediately before entry.
        _check_running(ctx)
    except BaseException as error:
        ctx.deps.recorder.stopped = error
        raise


class OwnershipCapability(AbstractCapability["RunnerDeps"]):
    """Fence graph/operation handlers, including re-entry by a business wrapper."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost", wrapped_by=[MessageRecordCapability])

    async def for_run(self, ctx: RunContext[RunnerDeps]):
        await check(ctx)
        return self

    async def wrap_run(self, ctx: RunContext[RunnerDeps], *, handler: WrapRunHandler):
        await check(ctx)
        return await handler()

    async def wrap_node_run(
        self, ctx: RunContext[RunnerDeps], *, node, handler: WrapNodeRunHandler[RunnerDeps]
    ):
        await check(ctx)
        return await handler(node)

    async def wrap_model_request(
        self, ctx: RunContext[RunnerDeps], *, request_context, handler: WrapModelRequestHandler
    ):
        await check(ctx)
        return await handler(request_context)

    async def wrap_tool_validate(
        self, ctx: RunContext[RunnerDeps], *, call, tool_def, args, handler: WrapToolValidateHandler
    ):
        await check(ctx)
        return await handler(args)

    async def wrap_tool_execute(
        self, ctx: RunContext[RunnerDeps], *, call, tool_def, args, handler: WrapToolExecuteHandler
    ):
        await check(ctx)
        return await handler(args)

    async def wrap_output_validate(
        self,
        ctx: RunContext[RunnerDeps],
        *,
        output,
        output_context,
        handler: WrapOutputValidateHandler,
    ):
        await check(ctx)
        return await handler(output)

    async def wrap_output_process(
        self,
        ctx: RunContext[RunnerDeps],
        *,
        output,
        output_context,
        handler: WrapOutputProcessHandler,
    ):
        await check(ctx)
        return await handler(output)


class _FencedOutputValidator(OutputValidator["RunnerDeps", str]):
    """Gate each registered validator; native invocation retains SDK semantics."""

    async def validate[T](self, result: T, run_context: RunContext[RunnerDeps]) -> T:
        await check(run_context)
        return await super().validate(result, run_context)


def _hook(callback):
    @wraps(callback)
    async def guarded(ctx, *args, **kwargs):
        await check(ctx)
        # Retain native sync execution, including SDK executor settings.
        return await _call_func(callback, ctx, *args, **kwargs)

    return guarded


def _stream(callback):
    @wraps(callback)
    async def guarded(ctx, *, stream):
        await check(ctx)
        events = callback(ctx, stream=stream)
        try:
            async for event in events:
                yield event
        finally:
            if close := getattr(events, "aclose", None):
                await close()

    return guarded


def _dispatch(method):
    @wraps(method)
    async def guarded(self, ctx, *args, **kwargs):
        implementation = getattr(type(self.wrapped), method.__name__)
        # Base no-ops start no business work. Hooks registrations are checked
        # individually, rather than just at the beginning of their dispatch loop.
        if implementation not in (
            getattr(AbstractCapability, method.__name__),
            getattr(Hooks, method.__name__),
        ):
            await check(ctx)
        return await method(self, ctx, *args, **kwargs)

    return guarded


class _FencedCapability(WrapperCapability["RunnerDeps"]):
    """Fence callbacks and replacements returned by native for_run.

    OwnershipCapability still gates physical operations: a wrapper cannot catch
    a stopped execution and restart its underlying handler. Native context
    managers and async-generator finally blocks retain resource teardown.
    """

    async def for_run(self, ctx: RunContext[RunnerDeps]):
        await check(ctx)
        wrapped = await self.wrapped.for_run(ctx)
        return self if wrapped is self.wrapped else fence(wrapped, resolved=True)

    before_run = _dispatch(WrapperCapability.before_run)
    after_run = _dispatch(WrapperCapability.after_run)
    wrap_run = _dispatch(WrapperCapability.wrap_run)
    on_run_error = _dispatch(WrapperCapability.on_run_error)
    before_node_run = _dispatch(WrapperCapability.before_node_run)
    after_node_run = _dispatch(WrapperCapability.after_node_run)
    wrap_node_run = _dispatch(WrapperCapability.wrap_node_run)
    on_node_run_error = _dispatch(WrapperCapability.on_node_run_error)
    before_model_request = _dispatch(WrapperCapability.before_model_request)
    after_model_request = _dispatch(WrapperCapability.after_model_request)
    wrap_model_request = _dispatch(WrapperCapability.wrap_model_request)
    on_model_request_error = _dispatch(WrapperCapability.on_model_request_error)
    prepare_tools = _dispatch(WrapperCapability.prepare_tools)
    prepare_output_tools = _dispatch(WrapperCapability.prepare_output_tools)
    before_tool_validate = _dispatch(WrapperCapability.before_tool_validate)
    after_tool_validate = _dispatch(WrapperCapability.after_tool_validate)
    wrap_tool_validate = _dispatch(WrapperCapability.wrap_tool_validate)
    on_tool_validate_error = _dispatch(WrapperCapability.on_tool_validate_error)
    before_tool_execute = _dispatch(WrapperCapability.before_tool_execute)
    after_tool_execute = _dispatch(WrapperCapability.after_tool_execute)
    wrap_tool_execute = _dispatch(WrapperCapability.wrap_tool_execute)
    on_tool_execute_error = _dispatch(WrapperCapability.on_tool_execute_error)
    before_output_validate = _dispatch(WrapperCapability.before_output_validate)
    after_output_validate = _dispatch(WrapperCapability.after_output_validate)
    wrap_output_validate = _dispatch(WrapperCapability.wrap_output_validate)
    on_output_validate_error = _dispatch(WrapperCapability.on_output_validate_error)
    before_output_process = _dispatch(WrapperCapability.before_output_process)
    after_output_process = _dispatch(WrapperCapability.after_output_process)
    wrap_output_process = _dispatch(WrapperCapability.wrap_output_process)
    on_output_process_error = _dispatch(WrapperCapability.on_output_process_error)
    handle_deferred_tool_calls = _dispatch(WrapperCapability.handle_deferred_tool_calls)
    resolve_model_id = _dispatch(WrapperCapability.resolve_model_id)
    on_event = _dispatch(WrapperCapability.on_event)

    async def wrap_run_event_stream(self, ctx, *, stream):
        if type(self.wrapped).wrap_run_event_stream is not AbstractCapability.wrap_run_event_stream:
            await check(ctx)
        events = super().wrap_run_event_stream(ctx, stream=stream)
        try:
            async for event in events:
                yield event
        finally:
            if close := getattr(events, "aclose", None):
                await close()


class _FencedHooks(Hooks[Any]):
    """Keep native dispatch, including registrations added to a run-local Hooks."""

    def _get(self, key):
        return [
            replace(entry, func=(_stream if key == "wrap_run_event_stream" else _hook)(entry.func))
            for entry in super()._get(key)
        ]


def _listener(callback):
    @wraps(callback)
    async def guarded(self, ctx, event):
        await check(ctx)
        return await callback(self, ctx, event)

    return guarded


@cache
def _callback_type(cls: type) -> type:
    """Cache only static method adapters, never deps, State or clients.

    Native marked-event dispatch binds class descriptors directly, bypassing
    instance attributes. A layout-compatible subclass fences each descriptor;
    caching avoids accumulating one SDK event-dispatch cache entry per run.
    """
    if cls.__dict__.get("_kapy_fenced_callbacks", False):
        return cls
    hooks = issubclass(cls, Hooks) and not issubclass(cls, _FencedHooks)
    methods = collect_on_event_methods(cls)
    if not hooks and not methods:
        return cls
    namespace: dict[str, Any] = {
        "__slots__": (),
        "__module__": __name__,
        "_kapy_fenced_callbacks": True,
    }
    for base in reversed(cls.__mro__):
        for name, method in base.__dict__.items():
            if any(method is listener for listener in methods):
                namespace[name] = on_event(*method.event_types)(_listener(method.func))
    bases = (_FencedHooks, cls) if hooks else (cls,)
    return type(f"Fenced{cls.__name__}", bases, namespace)


def _callbacks(capability):
    result = copy(capability)
    result.__class__ = _callback_type(type(capability))
    if isinstance(result, Hooks):
        result._registry = {kind: list(entries) for kind, entries in result._registry.items()}
        # Hooks.on is a cached registration namespace bound to its original object.
        result.__dict__.pop("on", None)
    return result


def fence(
    capability: AbstractCapability[Any], *, resolved: bool = False
) -> AbstractCapability[Any]:
    """Fence declarations or an already-resolved native for_run replacement.

    Layer selection is prepared only before for_run. Its returned wrapper already
    holds the selected, rebound capabilities and must keep that composition.
    """
    # This internal gate already checks every implemented entry itself. Only the
    # exact internal type is exempt; business capabilities still need wrapping.
    if type(capability) is OwnershipCapability:
        return capability
    if isinstance(capability, (_FencedCapability, MessageRecordCapability)):
        return capability
    if isinstance(capability, CombinedCapability):
        result = capability.visit_and_replace(lambda child: fence(child, resolved=resolved))
        assert result is not None
        return result
    capability = _callbacks(capability)
    if isinstance(capability, LayerRegistry) and not resolved:
        # Selection calls installed leaves' for_run internally. Fence definitions
        # before selection, rather than only its returned composition.
        def factory(definition):
            def make(layer_id, access):
                return replace(definition, capability=fence(definition.capability))

            return make

        capability = LayerRegistry(
            {name: factory(definition) for name, definition in capability.definitions.items()},
            get_layers=lambda deps: deps.layers,
        )
    elif isinstance(capability, WrapperCapability):
        capability.wrapped = fence(capability.wrapped, resolved=resolved)
    return _FencedCapability(capability)


def fenced_agent(agent: Agent[RunnerDeps, str]) -> Agent[RunnerDeps, str]:
    """Borrow static settings/tools, but bind an independently fenced runtime tree.

    Copying retains public Agent configuration (instructions, output validation,
    registered tools) without reconstructing it or modifying a concurrent run.
    Native for_run still resolves each capability once for the new SDK run.
    """
    result = copy(agent)
    root = fence(agent.root_capability)
    assert isinstance(root, CombinedCapability)
    result._root_capability = root.for_agent(result)
    result._event_hooks = _callbacks(agent._event_hooks)
    result._output_validators = [
        _FencedOutputValidator(validator.function) for validator in agent._output_validators
    ]
    return result
