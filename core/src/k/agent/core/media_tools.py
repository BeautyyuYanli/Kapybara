"""Media normalization helpers and tool adapters for `pydantic_ai.Agent`.

`read_media` resolves local paths or remote URLs into local `BinaryContent`.
The remote path stays SSRF-hardened: every download hop is validated against
non-public/private destinations before bytes are fetched.

Compatibility is policy-driven rather than hard-coded. A `MediaPolicy`
declares which MIME types a caller can consume directly and which unsupported
types can be converted into a supported representation. Policy loading lives in
`k.agent.core.multimodal`; this module only applies the selected policy.

The plain `read_media(...)` helper keeps working for direct callers and uses the
built-in default preset (`google latest`). The agent-facing
`read_media_tool(...)` wrapper reads the explicit policy from deps.

Video files remain opt-in. If the selected policy does not explicitly support or
convert them, `read_media` rejects the input and points the agent to the
dedicated `read-video` workflow.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import mimetypes
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urljoin, urlparse

import filetype
import httpx
from pydantic_ai import BinaryContent, MultiModalContent, RunContext

from k.agent.core.entities import tool_exception_guard
from k.agent.core.multimodal import (
    MediaConversionRule,
    MediaPolicy,
    load_media_policy,
    normalize_media_type,
)
from k.config import MultimodalConfig

REMOTE_DOWNLOAD_LIMIT_BYTES: int = 50 * 1024 * 1024
REMOTE_REDIRECT_LIMIT: int = 5
REMOTE_TIMEOUT: httpx.Timeout = httpx.Timeout(20.0, connect=5.0)
REMOTE_USER_AGENT: str = "kapybara-read-media/1.0"


@dataclass(slots=True)
class PreparedFile:
    """Local file plus the best media-type hint known for it."""

    path: Path
    media_type_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured subprocess result for ffmpeg/ffprobe helpers."""

    stdout: bytes
    stderr: bytes


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _detect_media_type(path: Path, hint: str | None = None) -> str:
    """Infer the real media type from bytes first, then fall back to hints/name."""

    kind = filetype.guess(path)
    if kind is not None:
        guessed = normalize_media_type(kind.mime)
        if guessed:
            return guessed

    hinted = normalize_media_type(hint)
    if hinted and hinted != "application/octet-stream":
        return hinted

    guessed, _ = mimetypes.guess_type(path.name)
    normalized_guess = normalize_media_type(guessed)
    if normalized_guess:
        return normalized_guess

    return "application/octet-stream"


async def _run_media_process(*args: str) -> ProcessResult:
    """Run one media command and return captured stdout/stderr.

    The helpers in this module rely on the system `ffmpeg` / `ffprobe`
    executables instead of Pillow so one codec/toolchain determines both media
    probing and conversions.
    """

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        return_code = process.returncode or 1
        raise CalledProcessError(return_code, args, output=stdout, stderr=stderr)
    return ProcessResult(stdout=stdout, stderr=stderr)


async def _probe_video_packet_count(path: Path) -> int:
    """Return the number of packets in the first video stream.

    GIF animation detection is intentionally packet-count based so static GIFs
    remain accepted under `static_only_media_types` while multi-frame GIFs are
    rejected without decoding them via Pillow.
    """

    result = await _run_media_process(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets",
        "-of",
        "json",
        os.fspath(path),
    )
    payload: dict[str, Any] = json.loads(result.stdout.decode("utf-8"))
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ValueError(f"Unsupported image file: {path}")

    packet_value = streams[0].get("nb_read_packets")
    if packet_value is None:
        raise ValueError(f"Unsupported image file: {path}")

    try:
        return int(packet_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Unsupported image file: {path}") from exc


async def _is_non_animated_gif(path: Path) -> bool:
    """Return whether the GIF is static enough for `static_only_media_types`."""

    return await _probe_video_packet_count(path) <= 1


async def _convert_image_to_webp(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert any decodable image to WEBP.

    Animated images are flattened to their first frame so the historical
    OpenAI-oriented policy keeps treating unsupported animated images as static
    WEBP inputs after conversion.
    """

    output_path = dst_dir / f"{path.stem or 'image'}.webp"
    try:
        await _run_media_process(
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            os.fspath(path),
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            os.fspath(output_path),
        )
    except CalledProcessError as exc:
        raise ValueError(f"Unsupported image file: {path}") from exc
    if not output_path.exists():
        raise ValueError(f"Unsupported image file: {path}")
    return PreparedFile(path=output_path, media_type_hint="image/webp")


async def _convert_image_to_jpeg(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert any decodable image to a single-frame JPEG via `ffmpeg`."""

    output_path = dst_dir / f"{path.stem or 'image'}.jpg"
    try:
        await _run_media_process(
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            os.fspath(path),
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-pix_fmt",
            "yuvj420p",
            os.fspath(output_path),
        )
    except CalledProcessError as exc:
        raise ValueError(f"Unsupported image file: {path}") from exc
    if not output_path.exists():
        raise ValueError(f"Unsupported image file: {path}")
    return PreparedFile(path=output_path, media_type_hint="image/jpeg")


async def _convert_audio_to_webm(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert decodable audio to WEBM via `ffmpeg`."""

    output_path = dst_dir / f"{path.stem or 'audio'}.webm"
    try:
        await _run_media_process(
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            os.fspath(path),
            "-vn",
            "-acodec",
            "libopus",
            os.fspath(output_path),
        )
    except CalledProcessError as exc:
        reason = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Failed to convert audio file {path}: {reason}") from exc
    if not output_path.exists():
        raise ValueError(f"Failed to convert audio file {path}: missing output file")
    return PreparedFile(path=output_path, media_type_hint="audio/webm")


async def _convert_audio_to_mpeg(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert decodable audio to MP3 via `ffmpeg`."""

    output_path = dst_dir / f"{path.stem or 'audio'}.mp3"
    try:
        await _run_media_process(
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            os.fspath(path),
            "-vn",
            "-codec:a",
            "libmp3lame",
            os.fspath(output_path),
        )
    except CalledProcessError as exc:
        reason = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Failed to convert audio file {path}: {reason}") from exc
    if not output_path.exists():
        raise ValueError(f"Failed to convert audio file {path}: missing output file")
    return PreparedFile(path=output_path, media_type_hint="audio/mpeg")


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
    normalized = normalize_media_type(media_type)
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
                media_type_hint=normalize_media_type(
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

    if rule.converter == "image_to_jpeg":
        return await _convert_image_to_jpeg(prepared.path, dst_dir)
    if rule.converter == "audio_to_mpeg":
        return await _convert_audio_to_mpeg(prepared.path, dst_dir)
    if rule.converter == "image_to_webp":
        return await _convert_image_to_webp(prepared.path, dst_dir)
    if rule.converter == "audio_to_webm":
        return await _convert_audio_to_webm(prepared.path, dst_dir)
    raise ValueError(f"Unsupported media conversion rule: {rule.converter}")


async def _is_directly_supported(
    prepared: PreparedFile, media_type: str, policy: MediaPolicy
) -> bool:
    """Check direct support, including static-only formats like GIF."""

    if media_type not in policy.supported_media_types:
        return False
    if media_type in policy.static_only_media_types:
        return await _is_non_animated_gif(prepared.path)
    return True


async def _normalize_media(
    prepared: PreparedFile, dst_dir: Path, policy: MediaPolicy
) -> PreparedFile:
    """Keep supported files as-is and convert unsupported-but-decodable inputs."""

    media_type = _detect_media_type(prepared.path, prepared.media_type_hint)
    if await _is_directly_supported(prepared, media_type, policy):
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


def _to_binary_content(prepared: PreparedFile, policy: MediaPolicy) -> BinaryContent:
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
    policy: MediaPolicy,
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
    policy: MediaPolicy | None = None,
) -> list[MultiModalContent] | str:
    """
    Read media files from URLs or local file paths.
    Note: This tool does not support video files. For video content, use the `read-video` skill first.

    Args:
        media: A list of URLs and/or local file paths.
    """

    return await _read_media_impl(
        media,
        policy=policy or load_media_policy(MultimodalConfig()),
    )


@tool_exception_guard
async def read_media_tool(
    ctx: RunContext[Any],
    media: list[str],
) -> list[MultiModalContent] | str:
    """
    Read media files from URLs or local file paths.
    Note: This tool does not support video files. For video content, use the `read-video` skill first.

    Args:
        media: A list of URLs and/or local file paths.
    """

    deps = getattr(ctx, "deps", None)
    policy = getattr(deps, "media_policy", None)
    if policy is None:
        policy = load_media_policy(MultimodalConfig())
    return await _read_media_impl(media, policy=policy)
