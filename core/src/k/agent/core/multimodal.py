"""Multimodal media policy definitions for `read_media`.

This module owns the runtime compatibility contract used by
`k.agent.core.media_tools` and the translation from `k.config.MultimodalConfig`
into concrete policies. Policy selection is explicit: callers choose a preset
name or embed a custom policy in config. There is no model-class auto resolver.

Provider presets intentionally describe the formats this tool should normalize
for current model families:

- `openai latest`: OpenAI image/audio/PDF formats.
- `google latest`: Gemini image/audio/PDF formats. This is the default.
- `anthropic latest`: Anthropic image/PDF formats.

The `include ...` presets are intentionally narrower than provider presets so a
caller can force all images to JPEG and/or all audio to MP3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from k.config import (
    MultimodalConfig,
    MultimodalConversionRuleConfig,
    MultimodalCustomPolicyConfig,
)

MediaConverter = Literal[
    "image_to_jpeg",
    "audio_to_mpeg",
    "image_to_webp",
    "audio_to_webm",
]


def normalize_media_type(media_type: str | None) -> str | None:
    """Canonicalize MIME aliases so policy checks use one vocabulary."""

    if not media_type:
        return None
    normalized = media_type.split(";", 1)[0].strip().lower()
    aliases: dict[str, str] = {
        "audio/mp3": "audio/mpeg",
        "audio/x-m4a": "audio/mp4",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
        "image/jpg": "image/jpeg",
    }
    return aliases.get(normalized, normalized)


@dataclass(frozen=True, slots=True)
class MediaConversionRule:
    """Convert unsupported source types into one supported target type."""

    converter: MediaConverter
    target_media_type: str
    source_media_types: frozenset[str] = frozenset()
    source_prefixes: frozenset[str] = frozenset()

    def matches(self, media_type: str) -> bool:
        return media_type in self.source_media_types or any(
            media_type.startswith(prefix) for prefix in self.source_prefixes
        )


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    """Runtime media compatibility contract for `read_media`."""

    name: str
    supported_media_types: frozenset[str]
    conversion_rules: tuple[MediaConversionRule, ...] = ()
    static_only_media_types: frozenset[str] = frozenset()


def _rule_from_config(config: MultimodalConversionRuleConfig) -> MediaConversionRule:
    return MediaConversionRule(
        converter=config.converter,
        target_media_type=normalize_media_type(config.target_media_type)
        or config.target_media_type,
        source_media_types=frozenset(
            normalized
            for item in config.source_media_types
            if (normalized := normalize_media_type(item))
        ),
        source_prefixes=frozenset(
            prefix.lower() for prefix in config.source_prefixes if prefix
        ),
    )


def _policy_from_config(config: MultimodalCustomPolicyConfig) -> MediaPolicy:
    return MediaPolicy(
        name=config.name,
        supported_media_types=frozenset(
            normalized
            for item in config.supported_media_types
            if (normalized := normalize_media_type(item))
        ),
        conversion_rules=tuple(
            _rule_from_config(rule) for rule in config.conversion_rules
        ),
        static_only_media_types=frozenset(
            normalized
            for item in config.static_only_media_types
            if (normalized := normalize_media_type(item))
        ),
    )


def _image_to_jpeg_rule() -> MediaConversionRule:
    return MediaConversionRule(
        converter="image_to_jpeg",
        target_media_type="image/jpeg",
        source_prefixes=frozenset({"image/"}),
    )


def _audio_to_mpeg_rule() -> MediaConversionRule:
    return MediaConversionRule(
        converter="audio_to_mpeg",
        target_media_type="audio/mpeg",
        source_prefixes=frozenset({"audio/"}),
    )


PRESET_MEDIA_POLICIES: dict[str, MediaPolicy] = {
    "no multimodal": MediaPolicy(
        name="no multimodal", supported_media_types=frozenset()
    ),
    "include image": MediaPolicy(
        name="include image",
        supported_media_types=frozenset({"image/jpeg"}),
        conversion_rules=(_image_to_jpeg_rule(),),
    ),
    "include audio": MediaPolicy(
        name="include audio",
        supported_media_types=frozenset({"audio/mpeg"}),
        conversion_rules=(_audio_to_mpeg_rule(),),
    ),
    "include image and audio": MediaPolicy(
        name="include image and audio",
        supported_media_types=frozenset({"image/jpeg", "audio/mpeg"}),
        conversion_rules=(_image_to_jpeg_rule(), _audio_to_mpeg_rule()),
    ),
    "openai latest": MediaPolicy(
        name="openai latest",
        supported_media_types=frozenset(
            {
                "application/pdf",
                "audio/mp4",
                "audio/mpeg",
                "audio/wav",
                "audio/webm",
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/webp",
            }
        ),
        conversion_rules=(_image_to_jpeg_rule(), _audio_to_mpeg_rule()),
        static_only_media_types=frozenset({"image/gif"}),
    ),
    "google latest": MediaPolicy(
        name="google latest",
        supported_media_types=frozenset(
            {
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
            }
        ),
        conversion_rules=(_image_to_jpeg_rule(), _audio_to_mpeg_rule()),
    ),
    "anthropic latest": MediaPolicy(
        name="anthropic latest",
        supported_media_types=frozenset(
            {
                "application/pdf",
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/webp",
            }
        ),
        conversion_rules=(_image_to_jpeg_rule(),),
        static_only_media_types=frozenset({"image/gif"}),
    ),
}

DEFAULT_MEDIA_POLICY_NAME = "google latest"


def load_media_policy(multimodal_config: MultimodalConfig | None) -> MediaPolicy:
    """Resolve the configured media policy from preset or embedded config."""

    if multimodal_config is None or multimodal_config.policy is None:
        return PRESET_MEDIA_POLICIES[DEFAULT_MEDIA_POLICY_NAME]

    policy = multimodal_config.policy
    if isinstance(policy, str):
        key = policy.strip().lower()
        try:
            return PRESET_MEDIA_POLICIES[key]
        except KeyError as exc:
            supported = ", ".join(sorted(PRESET_MEDIA_POLICIES))
            raise ValueError(
                f"Unknown multimodal policy preset '{policy}'. Expected one of: {supported}."
            ) from exc

    return _policy_from_config(policy)
