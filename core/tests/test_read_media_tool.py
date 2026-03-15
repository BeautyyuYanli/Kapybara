from __future__ import annotations

import wave
from pathlib import Path

import pytest
from PIL import Image
from pydantic_ai import BinaryContent

from k.agent.core import media_tools
from k.agent.core.agent import read_media


@pytest.mark.anyio
async def test_read_media_downloads_remote_media_to_binary_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_file = tmp_path / "remote.jpg"
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(remote_file, format="JPEG")

    async def fake_download(url: str, dst_dir: Path) -> media_tools.PreparedFile:
        copied = dst_dir / "downloaded.jpg"
        copied.write_bytes(remote_file.read_bytes())
        return media_tools.PreparedFile(path=copied, media_type_hint="image/jpeg")

    monkeypatch.setattr(media_tools, "_download_remote_media", fake_download)

    out = await read_media(["https://example.com/a.jpg"])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "image/jpeg"
    assert out[0].data == remote_file.read_bytes()


@pytest.mark.anyio
async def test_read_media_rejects_kind_prefixes() -> None:
    out = await read_media(["audio:https://example.com/stream"])

    assert (
        out
        == "Invalid media spec: kind prefixes like 'image:https://...' are not supported; pass the URL/path directly."
    )


@pytest.mark.anyio
async def test_read_media_expands_env_vars_for_local_paths_and_converts_text_to_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("K_TEST_MEDIA_DIR", str(tmp_path))

    out = await read_media(["$K_TEST_MEDIA_DIR/x.txt"])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "application/pdf"
    assert out[0].data.startswith(b"%PDF-1.4")


@pytest.mark.anyio
async def test_read_media_converts_unsupported_image_to_png(tmp_path: Path) -> None:
    bmp_path = tmp_path / "sample.bmp"
    Image.new("RGB", (3, 3), color=(0, 255, 0)).save(bmp_path, format="BMP")

    out = await read_media([str(bmp_path)])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "image/png"
    assert out[0].data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.anyio
async def test_read_media_converts_unsupported_audio_to_wav(tmp_path: Path) -> None:
    src_path = tmp_path / "tone.au"
    with wave.open(str(src_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\x00\x00" * 800)

    out = await read_media([str(src_path)])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "audio/wav"
    assert out[0].data.startswith(b"RIFF")


def test_assert_public_remote_target_rejects_loopback() -> None:
    with pytest.raises(ValueError, match="non-public IP"):
        media_tools._assert_public_remote_target("http://127.0.0.1/file.png")


@pytest.mark.anyio
async def test_read_media_rejects_unknown_binary_file(tmp_path: Path) -> None:
    unknown_path = tmp_path / "unknown"
    unknown_path.write_bytes(b"\x00\x01\x02\x03\x04")

    out = await read_media([str(unknown_path)])

    assert (
        out
        == "Unsupported media type for OpenAI multimodal input: application/octet-stream. Supported types are PDF, PNG, JPEG, WEBP, non-animated GIF, and common audio inputs (MP3, WAV, M4A, WEBM)."
    )


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
