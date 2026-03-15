"""Media-related tools for `pydantic_ai.Agent`.

`read_media` normalizes every input into local `BinaryContent` before handing it
to the model. That keeps the behavior consistent across local files and remote
URLs, and it lets us enforce one compatibility policy based on OpenAI's
multimodal input baseline:

- images: PNG, JPEG, WEBP, or non-animated GIF
- audio: MP3/MPEG, WAV, M4A/MP4, or WEBM
- documents: PDF

Remote URLs are downloaded through an SSRF-hardened path that rejects
non-global/private destinations and validates every redirect target. Inputs that
are not already compatible are converted when we can do so without inventing
content:

- decodable images -> PNG
- decodable audio -> WAV (via `ffmpeg`)
- text-like documents -> PDF

Video files are intentionally rejected here. The agent should use the dedicated
video workflow first because "convert video to something else" is not a safe
default for user intent.
"""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlparse

import filetype
import httpx
from PIL import Image, UnidentifiedImageError
from pydantic_ai import BinaryContent, MultiModalContent

from k.agent.core.entities import tool_exception_guard

OPENAI_IMAGE_MEDIA_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
OPENAI_AUDIO_MEDIA_TYPES = {
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/webm",
}
OPENAI_DOCUMENT_MEDIA_TYPES = {"application/pdf"}
OPENAI_MEDIA_TYPES = (
    OPENAI_IMAGE_MEDIA_TYPES | OPENAI_AUDIO_MEDIA_TYPES | OPENAI_DOCUMENT_MEDIA_TYPES
)
TEXTUAL_DOCUMENT_MEDIA_TYPES = {
    "application/json",
    "application/ld+json",
    "application/rtf",
    "application/xml",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
}
REMOTE_DOWNLOAD_LIMIT_BYTES = 50 * 1024 * 1024
REMOTE_REDIRECT_LIMIT = 5
REMOTE_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
REMOTE_USER_AGENT = "kapybara-read-media/1.0"


@dataclass(slots=True)
class PreparedFile:
    """Local file plus the best media-type hint known for it."""

    path: Path
    media_type_hint: str | None = None


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


def _looks_like_text(data: bytes) -> bool:
    """Treat UTF-8 or mostly printable bytes as text-like content."""

    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        decoded = data.decode("utf-8")
        control_chars = sum(
            ord(char) < 32 and char not in "\t\n\r\f\b" for char in decoded
        )
        return control_chars == 0
    except UnicodeDecodeError:
        pass

    printable = sum(byte in b"\t\n\r\f\b" or 32 <= byte <= 126 for byte in data[:2048])
    sample_size = min(len(data), 2048)
    return sample_size > 0 and printable / sample_size >= 0.95


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

    head = path.read_bytes()[:2048]
    if _looks_like_text(head):
        return "text/plain"

    return "application/octet-stream"


def _is_non_animated_gif(path: Path) -> bool:
    """OpenAI accepts GIF input, but only when it is not animated."""

    with Image.open(path) as image:
        return getattr(image, "n_frames", 1) <= 1


def _convert_image_to_png(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert any decodable image to PNG.

    Animated images are flattened to their first frame. That preserves visual
    content while avoiding unsupported animated formats.
    """

    output_path = dst_dir / f"{path.stem or 'image'}.png"
    try:
        with Image.open(path) as image:
            frame = image.convert("RGBA")
            frame.save(output_path, format="PNG")
    except UnidentifiedImageError as exc:
        raise ValueError(f"Unsupported image file: {path}") from exc
    return PreparedFile(path=output_path, media_type_hint="image/png")


async def _convert_audio_to_wav(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert decodable audio to WAV via `ffmpeg`.

    `ffmpeg` is used instead of a Python codec stack because the repo already
    relies on the host runtime and this keeps support broad without pulling in
    many optional decoder wheels.
    """

    output_path = dst_dir / f"{path.stem or 'audio'}.wav"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        os.fspath(path),
        os.fspath(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0 or not output_path.exists():
        reason = stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Failed to convert audio file {path}: {reason}")
    return PreparedFile(path=output_path, media_type_hint="audio/wav")


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_text_pdf_bytes(text: str) -> bytes:
    """Render plain text into a minimal PDF using the built-in Helvetica font."""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    page_width = 612
    page_height = 792
    left_margin = 72
    top_margin = 72
    line_height = 14
    lines_per_page = max(1, (page_height - 2 * top_margin) // line_height)
    pages = [
        lines[index : index + lines_per_page]
        for index in range(0, max(len(lines), 1), lines_per_page)
    ]
    if not pages:
        pages = [[]]

    objects: list[bytes] = []

    def add_object(payload: str | bytes) -> int:
        index = len(objects) + 1
        body = payload.encode("latin-1") if isinstance(payload, str) else payload
        objects.append(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
        return index

    font_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []

    for page_lines in pages:
        content_lines = [
            "BT",
            "/F1 12 Tf",
            f"{left_margin} {page_height - top_margin} Td",
        ]
        for idx, line in enumerate(page_lines):
            if idx > 0:
                content_lines.append(f"0 -{line_height} Td")
            safe = _escape_pdf_text(
                line.encode("latin-1", errors="replace").decode("latin-1")
            )
            content_lines.append(f"({safe}) Tj")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1")
        content_id = add_object(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        content_ids.append(content_id)
        page_id = add_object("")
        page_ids.append(page_id)

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    pages_id = add_object(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
    catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

    for page_id, content_id in zip(page_ids, content_ids, strict=True):
        objects[page_id - 1] = (
            f"{page_id} 0 obj\n"
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>\n"
            "endobj\n"
        ).encode("latin-1")

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    output = bytearray(header)
    offsets = [0]
    for obj in objects:
        offsets.append(len(output))
        output.extend(obj)
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("latin-1")
    )
    return bytes(output)


def _convert_text_to_pdf(path: Path, dst_dir: Path) -> PreparedFile:
    """Convert text-like files into a simple PDF to match OpenAI document input."""

    text = path.read_text(encoding="utf-8", errors="replace")
    output_path = dst_dir / f"{path.stem or 'document'}.pdf"
    output_path.write_bytes(_build_text_pdf_bytes(text))
    return PreparedFile(path=output_path, media_type_hint="application/pdf")


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


async def _normalize_to_openai_media(
    prepared: PreparedFile, dst_dir: Path
) -> PreparedFile:
    """Keep supported files as-is and convert unsupported-but-decodable inputs."""

    media_type = _detect_media_type(prepared.path, prepared.media_type_hint)
    if media_type in OPENAI_DOCUMENT_MEDIA_TYPES:
        return PreparedFile(path=prepared.path, media_type_hint=media_type)
    if media_type in OPENAI_AUDIO_MEDIA_TYPES:
        return PreparedFile(path=prepared.path, media_type_hint=media_type)
    if media_type in OPENAI_IMAGE_MEDIA_TYPES:
        if media_type == "image/gif" and not _is_non_animated_gif(prepared.path):
            return _convert_image_to_png(prepared.path, dst_dir)
        return PreparedFile(path=prepared.path, media_type_hint=media_type)

    if media_type.startswith("image/"):
        return _convert_image_to_png(prepared.path, dst_dir)
    if media_type.startswith("audio/"):
        return await _convert_audio_to_wav(prepared.path, dst_dir)
    if media_type.startswith("video/"):
        raise ValueError(
            "Video files are not supported by read_media; use the `read-video` skill first."
        )
    if media_type in TEXTUAL_DOCUMENT_MEDIA_TYPES or media_type.startswith("text/"):
        return _convert_text_to_pdf(prepared.path, dst_dir)

    raise ValueError(
        "Unsupported media type for OpenAI multimodal input: "
        f"{media_type}. Supported types are PDF, PNG, JPEG, WEBP, non-animated GIF, "
        "and common audio inputs (MP3, WAV, M4A, WEBM)."
    )


def _to_binary_content(prepared: PreparedFile) -> BinaryContent:
    """Read the normalized local file into `BinaryContent` with an explicit type."""

    media_type = _detect_media_type(prepared.path, prepared.media_type_hint)
    if media_type not in OPENAI_MEDIA_TYPES:
        raise ValueError(f"Normalized media file is still unsupported: {media_type}")
    return BinaryContent(data=prepared.path.read_bytes(), media_type=media_type)


@tool_exception_guard
async def read_media[DepsT](
    media: list[str],
) -> list[MultiModalContent] | str:
    """Read media from URLs or local files and normalize it for OpenAI input.

    Args:
        media: URLs or local file paths. URL inputs are downloaded to a temporary
            file through the SSRF-safe fetch path before compatibility checks run.

    Returns:
        A list of `BinaryContent` values whose media types are already accepted
        by the OpenAI multimodal API baseline.
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
            normalized = await _normalize_to_openai_media(prepared, temp_dir)
            results.append(_to_binary_content(normalized))
    return results
