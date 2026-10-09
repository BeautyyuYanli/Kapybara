"""Data adapters registered on native ``Hooks``; scheduling stays in the SDK."""

import json
from dataclasses import replace
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import CapabilityOrdering, Hooks
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessagesTypeAdapter,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.output import OutputContext
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from .contracts import HookSpec, KeepReply, UnhandledReply, ValueReply
from .rpc import Binding, json_value
from .validation import args_adapter

_CALL = TypeAdapter(ToolCallPart)
_TOOL = TypeAdapter(ToolDefinition)
_TOOLS = TypeAdapter(list[ToolDefinition])
_RESPONSE = TypeAdapter(ModelResponse)
_RAW = TypeAdapter(str | dict[str, JsonValue], config=ConfigDict(strict=True, allow_inf_nan=False))
_ARGS = TypeAdapter(dict[str, JsonValue], config=ConfigDict(strict=True, allow_inf_nan=False))


class _RequestData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[Any]
    # Provider extensions share the SDK's settings mapping without belonging
    # to its base TypedDict. Preserve every JSON field without a provider import.
    model_settings: dict[str, JsonValue] | None


class _RpcHook:
    """One remote handler with typed callbacks; only its declared point is registered."""

    def __init__(self, binding: Binding, spec: HookSpec) -> None:
        self.binding = binding
        self.spec = spec

    def dump(self, adapter: TypeAdapter[Any], value: Any) -> JsonValue:
        try:
            return json_value(adapter.dump_python(value, mode="json", warnings="error"))
        except (ValueError, TypeError) as error:
            raise self.binding.data_error(
                "SDK input cannot be represented as JSON", self.spec.handler, self.spec.point
            ) from error

    def parse(self, adapter: TypeAdapter[Any], value: JsonValue) -> Any:
        try:
            # Dataclass timestamps and other SDK wire types use their JSON codecs.
            return adapter.validate_json(json.dumps(value, allow_nan=False), strict=True)
        except (ValueError, TypeError) as error:
            raise self.binding.data_error(
                "invalid hook replacement", self.spec.handler, self.spec.point
            ) from error

    async def value(
        self, ctx: RunContext[Any], payload: dict[str, Any], *, error: Exception | None = None
    ) -> tuple[bool, JsonValue]:
        reply = await self.binding.invoke(ctx, self.spec.handler, payload, point=self.spec.point)
        if isinstance(reply, ValueReply):
            return True, reply.value
        if isinstance(reply, UnhandledReply) and error is not None:
            raise error
        if isinstance(reply, KeepReply) and error is None:
            return False, None
        raise self.binding.data_error(
            "reply kind is not allowed for this hook", self.spec.handler, self.spec.point
        )

    async def observe(self, ctx: RunContext[Any], payload: dict[str, Any]) -> None:
        reply = await self.binding.invoke(ctx, self.spec.handler, payload, point=self.spec.point)
        if not isinstance(reply, ValueReply) or reply.value is not None:
            raise self.binding.data_error(
                "observation hooks require value(null)", self.spec.handler, self.spec.point
            )

    def request(self, request_context: ModelRequestContext) -> dict[str, Any]:
        return {
            "messages": self.dump(ModelMessagesTypeAdapter, request_context.messages),
            "model_settings": request_context.model_settings,
        }

    def tool(self, call: ToolCallPart, tool_def: ToolDefinition, args: Any) -> dict[str, Any]:
        return {
            "call": self.dump(_CALL, call),
            "tool": self.dump(_TOOL, tool_def),
            "args": args,
        }

    @staticmethod
    def error(error: Exception) -> dict[str, str]:
        return {"type_name": type(error).__name__, "message": str(error)}

    async def before_run(self, ctx: RunContext[Any]) -> None:
        await self.observe(ctx, {})

    async def prepare_tools(
        self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        changed, value = await self.value(ctx, {"tools": self.dump(_TOOLS, tool_defs)})
        if not changed:
            return tool_defs
        replacements: list[ToolDefinition] = self.parse(_TOOLS, value)
        originals = {tool.name: tool for tool in tool_defs}
        names: set[str] = set()
        result: list[ToolDefinition] = []
        for replacement in replacements:
            original = originals.get(replacement.name)
            if (
                original is None
                or replacement.name in names
                or replace(replacement, description=original.description) != original
            ):
                raise self.binding.data_error(
                    "prepare hooks may only filter tools or change descriptions",
                    self.spec.handler,
                    self.spec.point,
                )
            names.add(replacement.name)
            result.append(replace(original, description=replacement.description))
        return result

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        changed, value = await self.value(ctx, {"request": self.request(request_context)})
        if not changed:
            return request_context
        data: _RequestData = self.parse(TypeAdapter(_RequestData), value)
        messages = self.parse(ModelMessagesTypeAdapter, json_value(data.messages))
        return replace(
            request_context,
            messages=messages,
            model_settings=cast(ModelSettings | None, data.model_settings),
        )

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        changed, value = await self.value(
            ctx,
            {"request": self.request(request_context), "response": self.dump(_RESPONSE, response)},
        )
        return self.parse(_RESPONSE, value) if changed else response

    async def on_model_request_error(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        _, value = await self.value(
            ctx, {"request": self.request(request_context), "error": self.error(error)}, error=error
        )
        return self.parse(_RESPONSE, value)

    async def before_tool_validate(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: str | dict[str, Any],
    ) -> str | dict[str, Any]:
        changed, value = await self.value(ctx, self.tool(call, tool_def, args))
        return self.parse(_RAW, value) if changed else args

    async def after_tool_validate(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        changed, value = await self.value(ctx, self.tool(call, tool_def, args))
        if not changed:
            return args
        replacement = self.parse(_ARGS, value)
        return args_adapter(tool_def.parameters_json_schema).validate_python(replacement)

    async def after_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        changed, value = await self.value(
            ctx, {**self.tool(call, tool_def, args), "result": result}
        )
        return value if changed else result

    async def on_tool_validate_error(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: str | dict[str, Any],
        error: ValidationError | ModelRetry,
    ) -> dict[str, Any]:
        _, value = await self.value(
            ctx, {**self.tool(call, tool_def, args), "error": self.error(error)}, error=error
        )
        replacement = self.parse(_ARGS, value)
        return args_adapter(tool_def.parameters_json_schema).validate_python(replacement)

    async def on_tool_execute_error(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        error: Exception,
    ) -> Any:
        _, value = await self.value(
            ctx, {**self.tool(call, tool_def, args), "error": self.error(error)}, error=error
        )
        return value

    async def before_output_validate(
        self, ctx: RunContext[Any], *, output_context: OutputContext, output: str | dict[str, Any]
    ) -> str | dict[str, Any]:
        changed, value = await self.value(ctx, {"output": output})
        return self.parse(_RAW, value) if changed else output

    async def on_event(self, ctx: RunContext[Any], event: AgentStreamEvent) -> None:
        # Custom and capability event families register new types after import.
        await self.observe(ctx, {"event": self.dump(TypeAdapter(AgentStreamEvent), event)})


def rpc_hooks(binding: Binding, spec: HookSpec, ordering: CapabilityOrdering) -> Hooks[Any]:
    """Register exactly one callback through the public native on namespace."""
    adapter = _RpcHook(binding, spec)
    hooks = Hooks[Any](id=f"{binding.plugin_id}.{spec.id}", ordering=ordering)
    match spec.point:
        case "before_run":
            hooks.on.before_run(adapter.before_run)
        case "prepare_tools":
            hooks.on.prepare_tools(adapter.prepare_tools)
        case "prepare_output_tools":
            hooks.on.prepare_output_tools(adapter.prepare_tools)
        case "before_model_request":
            hooks.on.before_model_request(adapter.before_model_request)
        case "after_model_request":
            hooks.on.after_model_request(adapter.after_model_request)
        case "on_model_request_error":
            hooks.on.model_request_error(adapter.on_model_request_error)
        case "before_tool_validate":
            hooks.on.before_tool_validate(adapter.before_tool_validate)
        case "after_tool_validate":
            hooks.on.after_tool_validate(adapter.after_tool_validate)
        case "before_tool_execute":
            hooks.on.before_tool_execute(adapter.after_tool_validate)
        case "after_tool_execute":
            hooks.on.after_tool_execute(adapter.after_tool_execute)
        case "on_tool_validate_error":
            hooks.on.tool_validate_error(adapter.on_tool_validate_error)
        case "on_tool_execute_error":
            hooks.on.tool_execute_error(adapter.on_tool_execute_error)
        case "before_output_validate":
            hooks.on.before_output_validate(adapter.before_output_validate)
        case "on_event":
            hooks.on.event(adapter.on_event)
    return hooks
