"""Runtime multimodal media policy definitions for `read_media`.

This module owns the compatibility contract used by
`k.agent.core.media_tools` and translates structured multimodal config from
`k.multimodal_config` into concrete runtime policies. Preset resolution stays
in the config layer so TOML/env validation happens before agent runtime code
consumes a structured policy object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from k.multimodal_config import (
    MultimodalConfig,
    MultimodalConversionRuleConfig,
    MultimodalCustomPolicyConfig,
    resolve_multimodal_policy_config,
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


def load_media_policy(multimodal_config: MultimodalConfig | None) -> MediaPolicy:
    """Resolve the configured media policy from preset or embedded config."""

    return _policy_from_config(resolve_multimodal_policy_config(multimodal_config))
