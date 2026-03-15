"""Application settings loaded from environment and structured config payloads.

`Config` is the single settings surface used across starters, runtime wiring,
and helper utilities. Nested models are used for structured sections so TOML-
style config and `K_...` environment overrides share one schema.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MultimodalConversionRuleConfig(BaseModel):
    """Embedded conversion rule for a custom multimodal media policy."""

    converter: Literal[
        "image_to_jpeg",
        "audio_to_mpeg",
        "image_to_webp",
        "audio_to_webm",
    ]
    target_media_type: str
    source_media_types: list[str] = Field(default_factory=list)
    source_prefixes: list[str] = Field(default_factory=list)


class MultimodalCustomPolicyConfig(BaseModel):
    """Inline custom media policy used when a preset is not sufficient."""

    name: str = "custom"
    supported_media_types: list[str] = Field(default_factory=list)
    conversion_rules: list[MultimodalConversionRuleConfig] = Field(default_factory=list)
    static_only_media_types: list[str] = Field(default_factory=list)


class MultimodalConfig(BaseModel):
    """Multimodal settings for `read_media`.

    `policy` may be a preset name such as `"google latest"` or an embedded
    custom policy object. Leaving it unset defaults to the Google preset.
    """

    policy: str | MultimodalCustomPolicyConfig | None = None

    @model_validator(mode="after")
    def _validate_policy(self) -> "MultimodalConfig":
        if isinstance(self.policy, str) and not self.policy.strip():
            raise ValueError("multimodal.policy must not be empty when provided.")
        return self


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="K_", env_nested_delimiter="__")

    fs_base: Path
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    basic_os_user: str = "k"
    basic_os_addr: str = "k-container"
    basic_os_port: int = 22
    basic_os_sshkey: Path = Path(".ssh/id_ed25519")
