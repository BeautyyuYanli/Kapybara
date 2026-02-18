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
from pydantic_ai import BinaryContent, MultiModalContent
from pydantic_ai.messages import AudioUrl, DocumentUrl, ImageUrl, VideoUrl

from k.agent.core.entities import tool_exception_guard


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
    if mt in {
        "application/json",
        "application/rtf",
        "application/xml",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
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


def _is_generic_binary_media_type(media_type: str) -> bool:
    mt = media_type.lower()
    return mt in {"application/octet-stream", "binary/octet-stream"}


async def _infer_url_kind(url: str) -> UrlMediaKind | None:
    path = urlparse(url).path
    guessed_type, _ = mimetypes.guess_type(path)
    if guessed_type:
        if _is_generic_binary_media_type(guessed_type):
            return None
        kind = _url_kind_from_media_type(guessed_type)
        if kind:
            return kind

    sniffed_type = await _sniff_url_media_type(url)
    if sniffed_type:
        if _is_generic_binary_media_type(sniffed_type):
            return None
        kind = _url_kind_from_media_type(sniffed_type)
        if kind:
            return kind

    return None


@tool_exception_guard
async def read_media[DepsT](
    media: list[str],
) -> list[MultiModalContent] | str:
    """
    Read media files from URLs or local file paths.

    Args:
        media: A list of URLs and/or local file paths.
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

        if _is_http_url(spec):
            url_kind = await _infer_url_kind(spec)
            if url_kind is None:
                raise ValueError("Invalid URL/path or not a supported media file.")
            if url_kind == "image-url":
                content = ImageUrl(url=spec)
            elif url_kind == "audio-url":
                content = AudioUrl(url=spec)
            elif url_kind == "video-url":
                content = VideoUrl(url=spec)
            else:
                content = DocumentUrl(url=spec)
            # Fail fast
            content._infer_media_type()
        else:
            expanded = os.path.expandvars(spec)
            path = Path(expanded).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"File not found: {path}")

            content = BinaryContent.from_path(path)


        results.append(content)
    return results
