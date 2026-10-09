"""JSON-only declarations and replies for the version 1 plugin protocol."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

type LocalId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")]
type HookId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")]
type HandlerId = Annotated[str, Field(min_length=1)]
type HookPoint = Literal[
    "before_run",
    "prepare_tools",
    "prepare_output_tools",
    "before_model_request",
    "after_model_request",
    "on_model_request_error",
    "before_tool_validate",
    "after_tool_validate",
    "before_tool_execute",
    "after_tool_execute",
    "on_tool_validate_error",
    "on_tool_execute_error",
    "before_output_validate",
    "on_event",
]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)


class TextInstructionSpec(_Contract):
    kind: Literal["text"]
    text: str


class RpcInstructionSpec(_Contract):
    kind: Literal["rpc"]
    handler: HandlerId


type InstructionSpec = Annotated[
    TextInstructionSpec | RpcInstructionSpec, Field(discriminator="kind")
]


class HookOrderingSpec(_Contract):
    position: Literal["outermost", "innermost"] | None = None
    wraps: list[HookId] = Field(default_factory=list)
    wrapped_by: list[HookId] = Field(default_factory=list)


class HookSpec(_Contract):
    id: LocalId
    point: HookPoint
    handler: HandlerId
    ordering: HookOrderingSpec | None = None


class PluginManifest(_Contract):
    protocol_version: Literal[1]
    plugin_id: LocalId
    instructions: list[InstructionSpec] = Field(default_factory=list)
    hooks: list[HookSpec] = Field(default_factory=list)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def _integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol_version must be the integer 1")
        return value


class ValueReply(_Contract):
    kind: Literal["value"]
    value: JsonValue


class KeepReply(_Contract):
    kind: Literal["keep"]


class UnhandledReply(_Contract):
    kind: Literal["unhandled"]


type InvokeReply = Annotated[ValueReply | KeepReply | UnhandledReply, Field(discriminator="kind")]


class PluginHostError(Exception):
    """Declaration, data, or RPC failure with the available export identity.

    Local errors retain the original exception in ``__cause__``. Across durable
    boundaries errors use the engine's native failure representation. SDK ordering
    and local tool argument validation keep their native exception types.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: Literal["declaration", "data", "rpc"],
        plugin_id: str | None = None,
        handler: str | None = None,
        point: str | None = None,
    ) -> None:
        self.phase = phase
        self.plugin_id = plugin_id
        self.handler = handler
        self.point = point
        identity = "/".join(part for part in (plugin_id, handler, point) if part)
        super().__init__(f"{phase}{f' [{identity}]' if identity else ''}: {message}")
