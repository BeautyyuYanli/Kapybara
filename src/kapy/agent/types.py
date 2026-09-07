"""Configuration and explicit script plugins for the agent runner."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, SecretStr

type AuthorizeWait = Callable[[UUID, tuple[UUID, ...]], Awaitable[None]]


@dataclass(frozen=True)
class RunnerConfig:
    base_url: str
    api_key: SecretStr
    context_window_tokens: int
    model: str = "gpt-5.6-luna"
    max_output_tokens: int = 16_384
    compression_ratio: float = 0.70
    keep_recent_ratio: float = 0.10
    media_max_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.base_url or not self.model.strip():
            raise ValueError("base_url and model must be nonempty")
        if not 0 < self.max_output_tokens < self.context_window_tokens:
            raise ValueError("Output token limit must be positive and below the context window")
        if not 0 < self.compression_ratio <= 1 or not 0 < self.keep_recent_ratio <= 1:
            raise ValueError("Compression ratios must be in (0, 1]")
        if not 0 < self.media_max_bytes <= 20 * 1024 * 1024:
            raise ValueError("Media limit must be positive and at most 20 MiB")


@dataclass(frozen=True)
class ProcessCommand:
    argv: tuple[str, ...]
    stdin: bytes | None = None
    cwd: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(arg, str) or "\x00" in arg for arg in self.argv):
            raise ValueError("Command requires nonempty argv without NUL bytes")


@dataclass(frozen=True)
class ScriptTool[P: BaseModel]:
    name: str
    description: str
    parameters: type[P]
    render: Callable[[P], ProcessCommand]

    def __post_init__(self) -> None:
        if "machine_id" in self.parameters.model_fields:
            raise ValueError("machine_id is a reserved plugin parameter")


class ContextBudgetExceeded(Exception):
    """The provider rejected context after bounded compression retries."""


class AgentResourceLimit(Exception):
    """A message or checkpoint exceeds the durable storage budget."""
