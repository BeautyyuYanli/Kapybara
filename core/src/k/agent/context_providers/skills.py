"""Skills-backed context provider.

This provider preserves the current skills grounding layout by reusing the same
`<BasicSkills>` and `<ChannelSkills>` wrappers that the legacy agent injects.
Runtime tools expose direct skill-doc reads without changing the underlying
`skills:` URI and channel-root resolution rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator

from k.agent.channels import channel_root, effective_out_channel
from k.agent.context_providers.base import (
    ContextProvider,
    GroundingItem,
    ProviderConfig,
    ToolSpec,
    TurnContext,
)
from k.agent.core.skills_md import (
    concat_skills_md,
    maybe_load_channel_skill_md,
    resolve_skills_uri,
    skills_root_from_config_base,
    skills_uri,
)


class SkillsProviderConfig(ProviderConfig):
    """Configuration for `SkillsProvider`."""

    config_base: Path
    skills_root: Path | None = None

    @field_validator("config_base", "skills_root")
    @classmethod
    def _normalize_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser().resolve()

    @property
    def resolved_skills_root(self) -> Path:
        if self.skills_root is not None:
            return self.skills_root
        return skills_root_from_config_base(self.config_base)


class ReadSkillDocumentInput(BaseModel):
    """Read one skill document using a path relative to the skills root."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str


class ReadChannelSkillDocumentInput(BaseModel):
    """Read one channel-root skill document, defaulting to the current turn."""

    model_config = ConfigDict(extra="forbid")

    group: str = "messager"
    channel: str | None = None


class SkillsProvider(ContextProvider[SkillsProviderConfig]):
    """Adapt the current skills prompt builder into a structured provider."""

    name = "skills"
    config_model = SkillsProviderConfig

    config: SkillsProviderConfig

    _PRIORITY = 300

    def __init__(self, *, config: SkillsProviderConfig) -> None:
        self.config = config

    @classmethod
    def from_config(cls, config: SkillsProviderConfig) -> Self:
        return cls(config=config)

    def supports(self, ctx: TurnContext) -> bool:
        return True

    def priority(self, ctx: TurnContext) -> int:
        return self._PRIORITY

    def build_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        blocks: list[GroundingItem] = []

        skills_md = concat_skills_md(self.config.config_base)
        if skills_md.strip():
            blocks.append(
                GroundingItem(content=f"<BasicSkills>{skills_md}</BasicSkills>")
            )

        channel = effective_out_channel(
            in_channel=ctx.in_channel,
            out_channel=ctx.out_channel,
        )
        channel_md = maybe_load_channel_skill_md(
            self.config.config_base,
            group="messager",
            channel=channel,
        )
        if channel_md:
            blocks.append(
                GroundingItem(content=f"<ChannelSkills>{channel_md}\n</ChannelSkills>")
            )

        return blocks

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="read_skill_document",
                description="Read one skill document by relative path.",
                input_schema=ReadSkillDocumentInput.model_json_schema(),
            ),
            ToolSpec(
                name="read_channel_skill_document",
                description="Read one channel-root skill document.",
                input_schema=ReadChannelSkillDocumentInput.model_json_schema(),
            ),
        ]

    def runtime_tool_names(self, ctx: TurnContext) -> list[str]:
        return ["read_skill_document", "read_channel_skill_document"]

    def execute_tool(self, name: str, payload: object, ctx: TurnContext) -> object:
        if name == "read_skill_document":
            parsed = ReadSkillDocumentInput.model_validate(payload)
            uri = skills_uri(parsed.relative_path)
            path = resolve_skills_uri(uri, skills_root=self.config.resolved_skills_root)
            return {
                "uri": uri,
                "content": path.read_text(encoding="utf-8"),
            }

        if name == "read_channel_skill_document":
            parsed = ReadChannelSkillDocumentInput.model_validate(payload)
            channel = parsed.channel or effective_out_channel(
                in_channel=ctx.in_channel,
                out_channel=ctx.out_channel,
            )
            root = channel_root(channel)
            relative_path = f"{parsed.group}/{root}/SKILLS.md"
            path = self.config.resolved_skills_root / parsed.group / root / "SKILLS.md"
            if not path.exists():
                return None
            return {
                "uri": skills_uri(relative_path),
                "content": path.read_text(encoding="utf-8"),
            }

        raise ValueError(f"Unknown skills tool: {name!r}")
