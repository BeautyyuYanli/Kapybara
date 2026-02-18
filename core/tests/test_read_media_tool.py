from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ImageUrl

from k.agent.core.agent import read_media


@pytest.mark.anyio
async def test_read_media_infers_url_kind_from_extension() -> None:
    out = await read_media(["https://example.com/a.jpg"])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], ImageUrl)
    assert out[0].url == "https://example.com/a.jpg"


@pytest.mark.anyio
async def test_read_media_rejects_kind_prefixes() -> None:
    out = await read_media(["audio:https://example.com/stream"])

    assert (
        out
        == "Invalid media spec: kind prefixes like 'image:https://...' are not supported; pass the URL/path directly."
    )


@pytest.mark.anyio
async def test_read_media_expands_env_vars_for_local_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.txt").write_bytes(b"hello")
    monkeypatch.setenv("K_TEST_MEDIA_DIR", str(tmp_path))

    out = await read_media(["$K_TEST_MEDIA_DIR/x.txt"])

    from pydantic_ai import BinaryContent

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "text/plain"


@pytest.mark.anyio
async def test_read_media_sniffs_local_content_without_extension(
    tmp_path: Path,
) -> None:
    # A PNG file without extension
    png_path = tmp_path / "mystery_file"
    png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"some data")

    out = await read_media([str(png_path)])

    from pydantic_ai import BinaryContent

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "application/octet-stream"


@pytest.mark.anyio
async def test_read_media_rejects_unknown_binary_file(tmp_path: Path) -> None:
    # A binary file with no magic bytes and no extension
    unknown_path = tmp_path / "unknown"
    unknown_path.write_bytes(b"\x00\x01\x02\x03\x04")

    out = await read_media([str(unknown_path)])

    from pydantic_ai import BinaryContent

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "application/octet-stream"


@pytest.mark.anyio
async def test_read_media_returns_text_error_for_missing_local_file(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "does_not_exist.png"
    out = await read_media([str(missing_path)])

    assert out == f"File not found: {missing_path}"


@pytest.mark.anyio
async def test_read_media_rejects_empty_strings() -> None:
    out = await read_media(["  "])

    assert out == "Invalid media spec: empty string"
