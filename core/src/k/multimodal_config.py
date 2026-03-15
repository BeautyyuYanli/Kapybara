"""Structured multimodal policy config and built-in presets.

This module separates static media-policy presets from the application-wide
`Config` settings loader in `k.config`. It owns the schema accepted from env or
TOML config and resolves preset names into structured policy objects before the
runtime media layer consumes them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


BUILTIN_MULTIMODAL_POLICY_PRESETS = MappingProxyType(
    {
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
)

DEFAULT_MEDIA_POLICY_NAME = "google latest"


class MultimodalConfig(BaseModel):
    """Multimodal settings for `read_media`.

    `policy` accepts either a preset name such as `"google latest"` or an
    embedded custom policy object. TOML config may therefore use either:

    - `[multimodal] policy = "google latest"`
    - `[multimodal.policy] ...`
    """

    policy: str | MultimodalCustomPolicyConfig | None = None

    @field_validator("policy", mode="before")
    @classmethod
    def _validate_policy(
        cls, value: str | dict[str, object] | MultimodalCustomPolicyConfig | None
    ) -> str | dict[str, object] | MultimodalCustomPolicyConfig | None:
        if not isinstance(value, str):
            return value

        policy_name = value.strip().lower()
        if not policy_name:
            raise ValueError("multimodal.policy must not be empty when provided.")
        if policy_name not in BUILTIN_MULTIMODAL_POLICY_PRESETS:
            supported = ", ".join(sorted(BUILTIN_MULTIMODAL_POLICY_PRESETS))
            raise ValueError(
                "Unknown multimodal policy preset "
                f"'{value}'. Expected one of: {supported}."
            )
        return policy_name


def resolve_multimodal_policy_config(
    multimodal_config: MultimodalConfig | None,
) -> MultimodalCustomPolicyConfig:
    """Resolve the configured multimodal policy to a structured policy object."""

    if multimodal_config is None or multimodal_config.policy is None:
        return BUILTIN_MULTIMODAL_POLICY_PRESETS[DEFAULT_MEDIA_POLICY_NAME]

    if isinstance(multimodal_config.policy, str):
        return BUILTIN_MULTIMODAL_POLICY_PRESETS[multimodal_config.policy]

    return multimodal_config.policy
