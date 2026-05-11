"""Memory-backed context provider.

This provider adapts the current memory injection and persistence helpers into
the structured context-provider interface without changing the legacy
`agent_run` pipeline. Grounding reuses the existing `<Memories>` prompt builder,
runtime tools expose lightweight read operations, and the finalization tool
persists a validated `MemoryRecord` through a `FolderMemoryStore` created from
validated provider config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from k.agent.contacts import resolve_contact_unique_ids
from k.agent.context_providers.base import (
    ContextProvider,
    GroundingItem,
    ProviderConfig,
    ToolSpec,
    TurnContext,
)
from k.agent.core.memory_injection import build_memory_injection_context
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.agent.memory.store import MemoryStore


class MemoryProviderConfig(ProviderConfig):
    """Configuration for `MemoryProvider`."""

    config_base: Path
    memory_root: Path | None = None

    @field_validator("config_base", "memory_root")
    @classmethod
    def _normalize_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser().resolve()

    @property
    def resolved_memory_root(self) -> Path:
        if self.memory_root is not None:
            return self.memory_root
        return memory_root_from_config_base(self.config_base)


class ListLatestMemoriesInput(BaseModel):
    """Read the latest memories within the current turn's input-channel subtree."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=5, gt=0)


class ReadMemoryInput(BaseModel):
    """Read one persisted memory record by id."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str


class AppendMemoryRecordInput(BaseModel):
    """Persist one fully formed `MemoryRecord` into the configured store."""

    model_config = ConfigDict(extra="forbid")

    record: MemoryRecord


class MemoryProvider(ContextProvider[MemoryProviderConfig]):
    """Adapt the current memory pipeline into the structured provider contract.

    Behaviour notes:
    - Grounding preserves the current `build_memory_injection_context()` logic,
      including platform-contact resolution through `contacts.json`.
    - Runtime tools intentionally stay small and deterministic so this provider
      does not invent retrieval semantics beyond what `MemoryStore` already
      supports.
    - Finalization exposes a single append tool that persists a validated
      `MemoryRecord` via `MemoryStore.append()`.
    """

    name = "memory"
    config_model = MemoryProviderConfig

    config: MemoryProviderConfig
    memory_store: MemoryStore

    _PRIORITY = 200

    def __init__(
        self,
        *,
        config: MemoryProviderConfig,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self.config = config
        self.memory_store = memory_store or FolderMemoryStore(
            root=config.resolved_memory_root
        )

    @classmethod
    def from_config(cls, config: MemoryProviderConfig) -> Self:
        return cls(config=config)

    def supports(self, ctx: TurnContext) -> bool:
        return True

    def priority(self, ctx: TurnContext) -> int:
        return self._PRIORITY

    def build_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        resolved_contacts = resolve_contact_unique_ids(
            config_base=self.config.config_base,
            platform_contacts=list(ctx.contacts),
        )
        injection = build_memory_injection_context(
            self.memory_store,
            in_channel=ctx.in_channel,
            contacts=resolved_contacts,
        )
        if not injection.injected_memories_prompt:
            return []
        return [GroundingItem(content=injection.injected_memories_prompt)]

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list_latest_memories",
                description="List recent memories in the current input-channel scope.",
                input_schema=ListLatestMemoriesInput.model_json_schema(),
            ),
            ToolSpec(
                name="read_memory",
                description="Read one persisted memory record by id.",
                input_schema=ReadMemoryInput.model_json_schema(),
            ),
            ToolSpec(
                name="append_memory_record",
                description="Persist one validated memory record.",
                input_schema=AppendMemoryRecordInput.model_json_schema(),
            ),
        ]

    def runtime_tool_names(self, ctx: TurnContext) -> list[str]:
        return ["list_latest_memories", "read_memory"]

    def finalization_tool_names(self, ctx: TurnContext) -> list[str]:
        return ["append_memory_record"]

    def execute_tool(self, name: str, payload: object, ctx: TurnContext) -> object:
        if name == "list_latest_memories":
            parsed = ListLatestMemoriesInput.model_validate(payload)
            return self.memory_store.get_latests(
                in_channel=ctx.in_channel,
                num=parsed.limit,
            )

        if name == "read_memory":
            parsed = ReadMemoryInput.model_validate(payload)
            record = self.memory_store.get_by_id(parsed.memory_id)
            if record is None:
                return None
            return record.model_dump(mode="json")

        if name == "append_memory_record":
            parsed = AppendMemoryRecordInput.model_validate(payload)
            self.memory_store.append(parsed.record)
            return {"id": parsed.record.id_}

        raise ValueError(f"Unknown memory tool: {name!r}")
