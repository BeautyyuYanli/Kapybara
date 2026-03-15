"""Media normalization helpers and tool adapters for `pydantic_ai.Agent`.

`read_media` resolves local paths or remote URLs into local `BinaryContent`.
The remote path stays SSRF-hardened: every download hop is validated against
non-public/private destinations before bytes are fetched.

Compatibility is policy-driven rather than hard-coded. A `ModelMediaPolicy`
declares which MIME types a model can consume directly and which unsupported
types can be converted into a supported representation. The default policy
matches the previous OpenAI-oriented behavior:

- images: PNG, JPEG, WEBP, or non-animated GIF
- audio: MP3/MPEG, WAV, M4A/MP4, or WEBM
- documents: PDF
- decodable images -> WEBP
- decodable audio -> WEBM (via `ffmpeg`)

The plain `read_media(...)` helper keeps working for direct callers and uses the
default OpenAI policy. The agent-facing `read_media_tool(...)` wrapper resolves a
policy from the current run model via deps/config so different providers can
opt into different multimodal capabilities without editing this module.

Video files remain opt-in. If the selected policy does not explicitly support or
convert them, `read_media` rejects the input and points the agent to the
dedicated `read-video` workflow.
"""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
import json
import mimetypes
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import filetype
import httpx
from PIL import Image, ImageSequence, UnidentifiedImageError
from pydantic import BaseModel, Field
from pydantic_ai import BinaryContent, MultiModalContent, RunContext
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel

from k.agent.core.entities import tool_exception_guard

REMOTE_DOWNLOAD_LIMIT_BYTES = 50 * 1024 * 1024
REMOTE_REDIRECT_LIMIT = 5
REMOTE_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
REMOTE_USER_AGENT = "kapybara-read-media/1.0"


@dataclass(slots=True)
class PreparedFile:
    """Local file plus the best media-type hint known for it."""

    path: Path
    media_type_hint: str | None = None


@dataclass(frozen=True, slots=True)
class MediaConversionRule:
    """Policy rule that converts unsupported MIME types into a supported one."""

    converter: Literal["image_to_webp", "audio_to_webm"]
    target_media_type: str
    source_media_types: frozenset[str] = frozenset()
    source_prefixes: frozenset[str] = frozenset()

    def matches(self, media_type: str) -> bool:
        return media_type in self.source_media_types or any(
            media_type.startswith(prefix) for prefix in self.source_prefixes
        )


@dataclass(frozen=True, slots=True)
class ModelMediaPolicy:
    """Model-specific media compatibility contract.

    `supported_media_types` are accepted as-is. `static_only_media_types` are
    accepted only when the file is non-animated (currently relevant for GIF).
    `conversion_rules` are checked in order when the detected type is not
    accepted directly.
    """

    name: str
    supported_media_types: frozenset[str]
    conversion_rules: tuple[MediaConversionRule, ...] = ()
    static_only_media_types: frozenset[str] = frozenset()


@dataclass(slots=True)
class ModelMediaPolicyResolver:
    """Resolve the active `ModelMediaPolicy` from the current run model.

    Resolution is based on the concrete model class instead of provider/name
    strings. When multiple configured classes match (for example
    `OpenRouterModel` also matching `OpenAIChatModel`), the nearest class in the
    model's MRO wins.

    Wrapper and fallback models are unwrapped before matching because media must
    be normalized for the underlying provider-facing model, not the adapter.
    """

    policies: dict[str, ModelMediaPolicy]
    default_policy_name: str
    model_type_policy_map: dict[type[object], str]

    def resolve(self, model: object | None = None) -> ModelMediaPolicy:
        policy_name = self._resolve_policy_name(model)

        try:
            return self.policies[policy_name]
        except KeyError as exc:
            raise ValueError(
                f"Unknown media policy '{policy_name}' selected for model {type(_unwrap_model_for_media_policy(model)).__name__}."
            ) from exc

    def _resolve_policy_name(self, model: object | None) -> str:
        candidate = _unwrap_model_for_media_policy(model)
        if candidate is None:
            return self.default_policy_name

        candidate_type = type(candidate)
        mro = candidate_type.mro()
        best_match: tuple[int, str] | None = None
        for model_type, policy_name in self.model_type_policy_map.items():
            if not isinstance(candidate, model_type):
                continue
            distance = mro.index(model_type)
            if best_match is None or distance < best_match[0]:
                best_match = (distance, policy_name)

        return best_match[1] if best_match is not None else self.default_policy_name


class MediaConversionRuleConfig(BaseModel):
    """JSON-serializable shape for `MediaConversionRule`."""

    converter: Literal["image_to_webp", "audio_to_webm"]
    target_media_type: str
    source_media_types: list[str] = Field(default_factory=list)
    source_prefixes: list[str] = Field(default_factory=list)

    def to_runtime(self) -> MediaConversionRule:
        return MediaConversionRule(
            converter=self.converter,
            target_media_type=_normalize_media_type(self.target_media_type)
            or self.target_media_type,
            source_media_types=frozenset(
                normalized
                for item in self.source_media_types
                if (normalized := _normalize_media_type(item))
            ),
            source_prefixes=frozenset(
                prefix.lower() for prefix in self.source_prefixes if prefix
            ),
        )


class ModelMediaPolicyConfig(BaseModel):
    """JSON-serializable shape for `ModelMediaPolicy`."""

    supported_media_types: list[str]
    conversion_rules: list[MediaConversionRuleConfig] = Field(default_factory=list)
    static_only_media_types: list[str] = Field(default_factory=list)

    def to_runtime(self, *, name: str) -> ModelMediaPolicy:
        return ModelMediaPolicy(
            name=name,
            supported_media_types=frozenset(
                normalized
                for item in self.supported_media_types
                if (normalized := _normalize_media_type(item))
            ),
            conversion_rules=tuple(rule.to_runtime() for rule in self.conversion_rules),
            static_only_media_types=frozenset(
                normalized
                for item in self.static_only_media_types
                if (normalized := _normalize_media_type(item))
            ),
        )


class ModelMediaPolicyResolverConfig(BaseModel):
    """External configuration for `ModelMediaPolicyResolver`.

    This is the bridge from file/env configuration into the runtime policy
    objects used by `read_media`.

    `model_type_policy_map` keys are import paths to model classes, for example
    `pydantic_ai.models.openrouter.OpenRouterModel`.
    """

    default_policy_name: str = "openai"
    policies: dict[str, ModelMediaPolicyConfig] = Field(default_factory=dict)
    model_type_policy_map: dict[str, str] = Field(default_factory=dict)

    def to_runtime(self) -> ModelMediaPolicyResolver:
        return ModelMediaPolicyResolver(
            policies={
                name: policy.to_runtime(name=name)
                for name, policy in self.policies.items()
            },
            default_policy_name=self.default_policy_name,
            model_type_policy_map={
                _load_model_type(model_type_path): policy_name
                for model_type_path, policy_name in self.model_type_policy_map.items()
            },
        )


OPENAI_MEDIA_POLICY = ModelMediaPolicy(
    name="openai",
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
    static_only_media_types=frozenset({"image/gif"}),
    conversion_rules=(
        MediaConversionRule(
            converter="image_to_webp",
            target_media_type="image/webp",
            source_prefixes=frozenset({"image/"}),
        ),
        MediaConversionRule(
            converter="audio_to_webm",
            target_media_type="audio/webm",
            source_prefixes=frozenset({"audio/"}),
        ),
    ),
)


def default_media_policy_resolver() -> ModelMediaPolicyResolver:
    """Return the built-in resolver used when no external config is provided."""

    return ModelMediaPolicyResolver(
        policies={"openai": OPENAI_MEDIA_POLICY},
        default_policy_name="openai",
        model_type_policy_map={},
    )


def load_media_policy_resolver(config_path: Path | None) -> ModelMediaPolicyResolver:
    """Load a resolver from JSON config, falling back to the built-in defaults."""

    if config_path is None:
        return default_media_policy_resolver()

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    config = ModelMediaPolicyResolverConfig.model_validate(payload)
    runtime = config.to_runtime()
    if "openai" not in runtime.policies:
        runtime.policies["openai"] = OPENAI_MEDIA_POLICY
    if not runtime.default_policy_name:
        runtime.default_policy_name = "openai"
    return runtime


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_media_type(media_type: str | None) -> str | None:
    """Canonicalize MIME aliases so compatibility checks use one vocabulary."""

    if not media_type:
        return None
    normalized = media_type.split(";", 1)[0].strip().lower()
    aliases = {
        "audio/mp3": "audio/mpeg",
        "audio/x-m4a": "audio/mp4",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
        "image/jpg": "image/jpeg",
    }
    return aliases.get(normalized, normalized)


def _unwrap_model_for_media_policy(model: object | None) -> object | None:
    """Return the concrete provider-facing model used for media normalization.

    Wrapper models simply delegate requests, so their wrapped model determines
    media compatibility. Fallback models normalize against the first configured
    model because media preparation happens before provider failover.
    """

    current = model
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, WrapperModel):
            current = current.wrapped
            continue
        if isinstance(current, FallbackModel):
            current = current.models[0] if current.models else None
            continue
        return current
    return current


def _load_model_type(import_path: str) -> type[object]:
    """Load a model class from a fully qualified import path."""

    module_name, separator, attr_name = import_path.rpartition(".")
    if not separator:
        raise ValueError(
            f"Invalid model type path '{import_path}'. Expected 'module.ClassName'."
        )

    model_type = getattr(importlib.import_module(module_name), attr_name)
    if not isinstance(model_type, type):
        raise ValueError(f"Configured model type '{import_path}' is not a class.")
    return model_type


def _detect_media_type(path: Path, hint: str | None = None) -> str:
    """Infer the real media type from bytes first, then fall back to hints/name."""

    kind = filetype.guess(path)
    if kind is not None:
        guessed = _normalize_media_type(kind.mime)
        if guessed:
            return guessed

    hinted = _normalize_media_type(hint)
    if hinted and hinted != "application/octet-stream":
        return hinted

    guessed, _ = mimetypes.guess_type(path.name)
    normalized_guess = _normalize_media_type(guessed)
    if normalized_guess:
        return normalized_guess

    return "application/octet-stream"


def _is_non_animated_gif(path: Path) -> bool:
    """Return whether the GIF is static enough for `static_only_media_types`."""

    with Image.open(path) as image:
        return getattr(image, "n_frames", 1) <= 1


def _convert_image_to_webp(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert any decodable image to WEBP.

    Animated images are flattened to their first frame.
    """

    output_path = dst_dir / f"{path.stem or 'image'}.webp"
    try:
        with Image.open(path) as image:
            if getattr(image, "is_animated", False):
                image = next(ImageSequence.Iterator(image))
            image.save(output_path, format="WEBP")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Unsupported image file: {path}") from exc
    return PreparedFile(path=output_path, media_type_hint="image/webp")


async def _convert_audio_to_webm(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert decodable audio to WEBM via `ffmpeg`."""

    output_path = dst_dir / f"{path.stem or 'audio'}.webm"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        os.fspath(path),
        "-vn",
        "-acodec",
        "libopus",
        os.fspath(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0 or not output_path.exists():
        reason = stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Failed to convert audio file {path}: {reason}")
    return PreparedFile(path=output_path, media_type_hint="audio/webm")


def _assert_public_remote_target(url: str) -> None:
    """Reject URLs that resolve to loopback/private/link-local/reserved IPs."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid remote media URL: {url}")
    if parsed.username or parsed.password:
        raise ValueError("Remote media URLs must not include credentials.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(
            f"Could not resolve remote media host: {parsed.hostname}"
        ) from exc

    for _family, _socktype, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise ValueError(
                "Remote media URL resolves to a non-public IP address, which is not allowed."
            )


def _filename_suffix(url: str, media_type: str | None) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix
    if suffix:
        return suffix
    normalized = _normalize_media_type(media_type)
    if normalized:
        guessed = mimetypes.guess_extension(normalized)
        if guessed:
            return guessed
    return ".bin"


async def _download_remote_media(url: str, dst_dir: Path) -> PreparedFile:
    """Download a remote media file after validating every hop for SSRF safety."""

    current_url = url
    redirects = 0
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=REMOTE_TIMEOUT,
        headers={"User-Agent": REMOTE_USER_AGENT, "Accept": "*/*"},
    ) as client:
        while True:
            _assert_public_remote_target(current_url)
            response = await client.get(current_url)

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError(
                        "Remote media redirect did not provide a Location header."
                    )
                redirects += 1
                if redirects > REMOTE_REDIRECT_LIMIT:
                    raise ValueError("Remote media URL exceeded the redirect limit.")
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()
            size = len(response.content)
            if size > REMOTE_DOWNLOAD_LIMIT_BYTES:
                raise ValueError(
                    f"Remote media file is too large ({size} bytes > {REMOTE_DOWNLOAD_LIMIT_BYTES} bytes)."
                )
            output_path = (
                dst_dir
                / f"remote{_filename_suffix(current_url, response.headers.get('Content-Type'))}"
            )
            output_path.write_bytes(response.content)
            return PreparedFile(
                path=output_path,
                media_type_hint=_normalize_media_type(
                    response.headers.get("Content-Type")
                ),
            )


async def _prepare_input_file(spec: str, dst_dir: Path) -> PreparedFile:
    """Resolve a user media spec into a local file plus media-type hint."""

    if _is_http_url(spec):
        return await _download_remote_media(spec, dst_dir)

    expanded = os.path.expandvars(spec)
    path = Path(expanded).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return PreparedFile(path=path)


async def _apply_conversion_rule(
    prepared: PreparedFile, dst_dir: Path, rule: MediaConversionRule
) -> PreparedFile:
    """Apply one configured conversion rule."""

    if rule.converter == "image_to_webp":
        return _convert_image_to_webp(prepared.path, dst_dir)
    if rule.converter == "audio_to_webm":
        return await _convert_audio_to_webm(prepared.path, dst_dir)
    raise ValueError(f"Unsupported media conversion rule: {rule.converter}")


def _is_directly_supported(
    prepared: PreparedFile, media_type: str, policy: ModelMediaPolicy
) -> bool:
    """Check direct support, including static-only formats like GIF."""

    if media_type not in policy.supported_media_types:
        return False
    if media_type in policy.static_only_media_types:
        return _is_non_animated_gif(prepared.path)
    return True


async def _normalize_media(
    prepared: PreparedFile, dst_dir: Path, policy: ModelMediaPolicy
) -> PreparedFile:
    """Keep supported files as-is and convert unsupported-but-decodable inputs."""

    media_type = _detect_media_type(prepared.path, prepared.media_type_hint)
    if _is_directly_supported(prepared, media_type, policy):
        return PreparedFile(path=prepared.path, media_type_hint=media_type)

    for rule in policy.conversion_rules:
        if rule.matches(media_type):
            converted = await _apply_conversion_rule(prepared, dst_dir, rule)
            converted_type = _detect_media_type(converted.path, rule.target_media_type)
            if converted_type not in policy.supported_media_types:
                raise ValueError(
                    f"Media conversion for {media_type} produced unsupported type {converted_type} for policy '{policy.name}'."
                )
            return converted

    if media_type.startswith("video/"):
        raise ValueError(
            "Video files are not supported by read_media; use the `read-video` skill first."
        )

    raise ValueError(
        f"Unsupported media type for policy '{policy.name}': {media_type}. "
        "Provide a directly supported MIME type or configure a conversion rule for it."
    )


def _to_binary_content(
    prepared: PreparedFile, policy: ModelMediaPolicy
) -> BinaryContent:
    """Read the normalized local file into `BinaryContent` with an explicit type."""

    media_type = _detect_media_type(prepared.path, prepared.media_type_hint)
    if media_type not in policy.supported_media_types:
        raise ValueError(
            f"Normalized media file is still unsupported by policy '{policy.name}': {media_type}"
        )
    return BinaryContent(data=prepared.path.read_bytes(), media_type=media_type)


async def _read_media_impl(
    media: list[str],
    *,
    policy: ModelMediaPolicy,
) -> list[MultiModalContent] | str:
    """Read media from URLs or local files and normalize it for one model policy.

    Args:
        media: URLs or local file paths to include as multimodal input.
        policy: The model-specific media support/conversion contract.
    """

    results: list[MultiModalContent] = []
    for raw in media:
        spec = raw.strip()
        if not spec:
            raise ValueError("Invalid media spec: empty string")

        if spec.lower().startswith(("image:", "audio:", "video:", "document:")):
            raise ValueError(
                "Invalid media spec: kind prefixes like 'image:https://...' are not supported; "
                "pass the URL/path directly."
            )

        with TemporaryDirectory(prefix="kapybara-read-media-") as tmp_dir:
            temp_dir = Path(tmp_dir)
            prepared = await _prepare_input_file(spec, temp_dir)
            normalized = await _normalize_media(prepared, temp_dir, policy)
            results.append(_to_binary_content(normalized, policy))
    return results


@tool_exception_guard
async def read_media(
    media: list[str],
    *,
    policy: ModelMediaPolicy | None = None,
) -> list[MultiModalContent] | str:
    """Direct-call helper that keeps the historical OpenAI-compatible behavior."""

    return await _read_media_impl(media, policy=policy or OPENAI_MEDIA_POLICY)


@tool_exception_guard
async def read_media_tool(
    ctx: RunContext[Any],
    media: list[str],
) -> list[MultiModalContent] | str:
    """Agent tool adapter that resolves policy from the current run context."""

    deps = getattr(ctx, "deps", None)
    resolver = getattr(deps, "media_policy_resolver", None)
    if resolver is None:
        resolver = default_media_policy_resolver()
    policy = resolver.resolve(getattr(ctx, "model", None))
    return await _read_media_impl(media, policy=policy)
