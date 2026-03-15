"""Application settings loaded from environment and structured config payloads.

`Config` is the single settings surface used across starters, runtime wiring,
and helper utilities. Nested models are used for structured sections so TOML-
style config and `K_...` environment overrides share one schema.

The multimodal section also owns provider media-policy presets. Keeping the
preset names and their structured definitions here means TOML config,
environment overrides, and runtime media loading all validate against the same
schema before agent code sees the selected policy.
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
        if isinstance(self.policy, str):
            policy_name = self.policy.strip().lower()
            if not policy_name:
                raise ValueError("multimodal.policy must not be empty when provided.")
            if policy_name not in PRESET_MEDIA_POLICIES:
                supported = ", ".join(sorted(PRESET_MEDIA_POLICIES))
                raise ValueError(
                    "Unknown multimodal policy preset "
                    f"'{self.policy}'. Expected one of: {supported}."
                )
        return self


def _image_to_jpeg_rule() -> MultimodalConversionRuleConfig:
    return MultimodalConversionRuleConfig(
        converter="image_to_jpeg",
        target_media_type="image/jpeg",
        source_prefixes=["image/"],
    )


def _image_to_webp_rule() -> MultimodalConversionRuleConfig:
    return MultimodalConversionRuleConfig(
        converter="image_to_webp",
        target_media_type="image/webp",
        source_prefixes=["image/"],
    )


def _audio_to_mpeg_rule() -> MultimodalConversionRuleConfig:
    return MultimodalConversionRuleConfig(
        converter="audio_to_mpeg",
        target_media_type="audio/mpeg",
        source_prefixes=["audio/"],
    )


def _audio_to_webm_rule() -> MultimodalConversionRuleConfig:
    return MultimodalConversionRuleConfig(
        converter="audio_to_webm",
        target_media_type="audio/webm",
        source_prefixes=["audio/"],
    )


PRESET_MEDIA_POLICIES: dict[str, MultimodalCustomPolicyConfig] = {
    "no multimodal": MultimodalCustomPolicyConfig(
        name="no multimodal",
    ),
    "include image": MultimodalCustomPolicyConfig(
        name="include image",
        supported_media_types=["image/jpeg"],
        conversion_rules=[_image_to_jpeg_rule()],
    ),
    "include audio": MultimodalCustomPolicyConfig(
        name="include audio",
        supported_media_types=["audio/mpeg"],
        conversion_rules=[_audio_to_mpeg_rule()],
    ),
    "include image and audio": MultimodalCustomPolicyConfig(
        name="include image and audio",
        supported_media_types=["image/jpeg", "audio/mpeg"],
        conversion_rules=[_image_to_jpeg_rule(), _audio_to_mpeg_rule()],
    ),
    "openai latest": MultimodalCustomPolicyConfig(
        name="openai latest",
        supported_media_types=[
            "application/pdf",
            "audio/mp4",
            "audio/mpeg",
            "audio/wav",
            "audio/webm",
            "image/gif",
            "image/jpeg",
            "image/png",
            "image/webp",
        ],
        conversion_rules=[_image_to_webp_rule(), _audio_to_webm_rule()],
        static_only_media_types=["image/gif"],
    ),
    "google latest": MultimodalCustomPolicyConfig(
        name="google latest",
        supported_media_types=[
            "application/pdf",
            "audio/aac",
            "audio/aiff",
            "audio/flac",
            "audio/mpeg",
            "audio/ogg",
            "audio/wav",
            "image/heic",
            "image/heif",
            "image/jpeg",
            "image/png",
            "image/webp",
        ],
        conversion_rules=[_image_to_webp_rule(), _audio_to_mpeg_rule()],
    ),
    "anthropic latest": MultimodalCustomPolicyConfig(
        name="anthropic latest",
        supported_media_types=[
            "application/pdf",
            "image/gif",
            "image/jpeg",
            "image/png",
            "image/webp",
        ],
        conversion_rules=[_image_to_webp_rule()],
        static_only_media_types=["image/gif"],
    ),
}

DEFAULT_MEDIA_POLICY_NAME = "google latest"


def resolve_multimodal_policy_config(
    multimodal_config: MultimodalConfig | None,
) -> MultimodalCustomPolicyConfig:
    """Resolve the configured multimodal policy to a structured policy object.

    This keeps preset-name handling in the config layer so TOML/environment
    configuration and runtime media loading use the same validation path.
    """

    if multimodal_config is None or multimodal_config.policy is None:
        return PRESET_MEDIA_POLICIES[DEFAULT_MEDIA_POLICY_NAME]

    if isinstance(multimodal_config.policy, str):
        return PRESET_MEDIA_POLICIES[multimodal_config.policy.strip().lower()]

    return multimodal_config.policy


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="K_", env_nested_delimiter="__")

    fs_base: Path
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    basic_os_user: str = "k"
    basic_os_addr: str = "k-container"
    basic_os_port: int = 22
    basic_os_sshkey: Path = Path(".ssh/id_ed25519")
