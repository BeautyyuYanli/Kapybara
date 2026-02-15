"""Media-related tools for `pydantic_ai.Agent`.

This module contains the `read_media` tool and its helpers.

Security notes:
    - Local paths: this tool reads arbitrary local files. If the caller may
      supply untrusted input, consider restricting paths to an allowlist base
      directory and enforcing a max file size.
    - URLs: this tool may make network requests (best-effort MIME sniffing).
      If the caller may supply untrusted input, consider disabling sniffing or
      enforcing an allowlist/denylist to mitigate SSRF.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic_ai import BinaryContent, ModelRetry, MultiModalContent, RunContext
from pydantic_ai.messages import AudioUrl, DocumentUrl, ImageUrl, VideoUrl


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


UrlMediaKind = Literal["image-url", "video-url", "audio-url", "document-url"]


def _url_kind_from_media_type(media_type: str) -> UrlMediaKind | None:
    mt = media_type.lower()
    if mt.startswith("image/"):
        return "image-url"
    if mt.startswith("audio/"):
        return "audio-url"
    if mt.startswith("video/"):
        return "video-url"
    if mt == "application/pdf" or mt.startswith("text/"):
        return "document-url"
    return None


async def _sniff_url_media_type(url: str) -> str | None:
    """
    Best-effort MIME sniffing for extensionless URLs using HTTP headers.

    We prefer HEAD and fall back to a streamed GET with a tiny range request.
    """

    timeout = httpx.Timeout(5.0, connect=5.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        try:
            head = await client.head(url, headers={"Accept": "*/*"})
            content_type = head.headers.get("Content-Type")
            if content_type:
                return content_type.split(";", 1)[0].strip().lower() or None
        except Exception:
            pass

        try:
            async with client.stream(
                "GET",
                url,
                headers={"Accept": "*/*", "Range": "bytes=0-0"},
            ) as resp:
                content_type = resp.headers.get("Content-Type")
                if content_type:
                    return content_type.split(";", 1)[0].strip().lower() or None
        except Exception:
            return None
    return None


async def _infer_url_kind(url: str) -> UrlMediaKind:
    path = urlparse(url).path
    guessed_type, _ = mimetypes.guess_type(path)
    if guessed_type:
        kind = _url_kind_from_media_type(guessed_type)
        if kind:
            return kind

    sniffed_type = await _sniff_url_media_type(url)
    if sniffed_type:
        kind = _url_kind_from_media_type(sniffed_type)
        if kind:
            return kind

    return "document-url"


async def read_media[DepsT](
    ctx: RunContext[DepsT],
    media: list[str],
) -> list[MultiModalContent]:
    """
    Read media files from URLs or local file paths.

    Args:
    - `media`: A list of URLs and/or local file paths.
    """

    del ctx
    results: list[MultiModalContent] = []
    for raw in media:
        raw = raw.strip()
        if not raw:
            raise ModelRetry("Invalid media spec: empty string")

        raw_lower = raw.lower()
        if raw_lower.startswith(("image:", "audio:", "video:", "document:")):
            raise ModelRetry(
                "Invalid media spec: kind prefixes like 'image:https://...' are not supported; "
                "pass the URL/path directly."
            )

        spec = raw.strip()
        if not spec:
            raise ModelRetry(f"Invalid media spec: {raw!r}")

        if _is_http_url(spec):
            try:
                url_kind = await _infer_url_kind(spec)
                if url_kind == "image-url":
                    content = ImageUrl(url=spec)
                elif url_kind == "audio-url":
                    content = AudioUrl(url=spec)
                elif url_kind == "video-url":
                    content = VideoUrl(url=spec)
                else:
                    content = DocumentUrl(url=spec)
            except Exception as e:
                raise ModelRetry(f"Failed to load URL {spec}: {e}") from e
        else:
            try:
                expanded = os.path.expandvars(spec)
                path = Path(expanded).expanduser()
                content = BinaryContent.from_path(path)
            except Exception as e:
                raise ModelRetry(f"Failed to read file {spec}: {e}") from e

        results.append(content)
    return results
