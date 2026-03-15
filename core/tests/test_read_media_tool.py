from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import BinaryContent
from pydantic_ai.models.openrouter import OpenRouterModel

from k.agent.core import media_tools
from k.agent.core.agent import read_media


def _ffmpeg_generate_media(path: Path, *args: str) -> None:
    """Create deterministic test media via ffmpeg so tests match runtime codecs."""

    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", *args, str(path)],
        check=True,
        capture_output=True,
    )


@pytest.mark.anyio
async def test_read_media_downloads_remote_media_to_binary_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_file = tmp_path / "remote.jpg"
    _ffmpeg_generate_media(
        remote_file, "-f", "lavfi", "-i", "color=c=red:s=2x2", "-frames:v", "1"
    )

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
async def test_read_media_expands_env_vars_for_local_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "x.jpg"
    _ffmpeg_generate_media(
        image_path, "-f", "lavfi", "-i", "color=c=blue:s=2x2", "-frames:v", "1"
    )
    monkeypatch.setenv("K_TEST_MEDIA_DIR", str(tmp_path))

    out = await read_media(["$K_TEST_MEDIA_DIR/x.jpg"])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "image/jpeg"
    assert out[0].data == image_path.read_bytes()


@pytest.mark.anyio
async def test_read_media_converts_unsupported_image_to_webp(tmp_path: Path) -> None:
    bmp_path = tmp_path / "sample.bmp"
    _ffmpeg_generate_media(
        bmp_path, "-f", "lavfi", "-i", "color=c=green:s=3x3", "-frames:v", "1"
    )

    out = await read_media([str(bmp_path)])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "image/webp"
    assert out[0].data[:4] == b"RIFF"
    assert out[0].data[8:12] == b"WEBP"


@pytest.mark.anyio
async def test_read_media_converts_unsupported_audio_to_webm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src_path = tmp_path / "tone.unknown"
    src_path.write_bytes(b"not-a-real-audio-container")

    async def fake_convert_audio_to_webm(
        path: Path, dst_dir: Path
    ) -> media_tools.PreparedFile:
        output = dst_dir / "tone.webm"
        output.write_bytes(b"\x1aE\xdf\xa3fake-webm")
        return media_tools.PreparedFile(path=output, media_type_hint="audio/webm")

    original_detect = media_tools._detect_media_type

    def fake_detect_media_type(path: Path, hint: str | None = None) -> str:
        if path == src_path:
            return "audio/ogg"
        return original_detect(path, hint)

    monkeypatch.setattr(
        media_tools, "_convert_audio_to_webm", fake_convert_audio_to_webm
    )
    monkeypatch.setattr(media_tools, "_detect_media_type", fake_detect_media_type)

    out = await read_media([str(src_path)])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "audio/webm"
    assert out[0].data.startswith(b"\x1aE\xdf\xa3")


def test_assert_public_remote_target_rejects_loopback() -> None:
    with pytest.raises(ValueError, match="non-public IP"):
        media_tools._assert_public_remote_target("http://127.0.0.1/file.png")


@pytest.mark.anyio
async def test_read_media_converts_animated_gif_to_webp(tmp_path: Path) -> None:
    animated_gif = tmp_path / "animated.gif"
    _ffmpeg_generate_media(
        animated_gif,
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=2x2:rate=2",
        "-t",
        "1.5",
    )

    out = await read_media([str(animated_gif)])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "image/webp"
    assert out[0].data[:4] == b"RIFF"
    assert out[0].data[8:12] == b"WEBP"


@pytest.mark.anyio
async def test_read_media_rejects_unknown_binary_file(tmp_path: Path) -> None:
    unknown_path = tmp_path / "unknown"
    unknown_path.write_bytes(b"\x00\x01\x02\x03\x04")

    out = await read_media([str(unknown_path)])

    assert (
        out
        == "Unsupported media type for policy 'openai': application/octet-stream. Provide a directly supported MIME type or configure a conversion rule for it."
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


@pytest.mark.anyio
async def test_read_media_tool_uses_policy_selected_from_current_model(
    tmp_path: Path,
) -> None:
    png_path = tmp_path / "sample.png"
    _ffmpeg_generate_media(
        png_path, "-f", "lavfi", "-i", "color=c=purple:s=2x2", "-frames:v", "1"
    )

    image_only_policy = media_tools.ModelMediaPolicy(
        name="image-only",
        supported_media_types=frozenset({"image/png"}),
    )
    resolver = media_tools.ModelMediaPolicyResolver(
        policies={
            "openai": media_tools.OPENAI_MEDIA_POLICY,
            "image-only": image_only_policy,
        },
        default_policy_name="openai",
        model_type_policy_map={OpenRouterModel: "image-only"},
    )
    ctx = SimpleNamespace(
        deps=SimpleNamespace(media_policy_resolver=resolver),
        model=OpenRouterModel("custom/image-only"),
    )

    out = await media_tools.read_media_tool(ctx, [str(png_path)])

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "image/png"


def test_load_media_policy_resolver_from_json_config(tmp_path: Path) -> None:
    config_path = tmp_path / "media-policy.json"
    config_path.write_text(
        json.dumps(
            {
                "default_policy_name": "image-only",
                "policies": {
                    "image-only": {
                        "supported_media_types": ["image/png"],
                        "conversion_rules": [],
                    }
                },
                "model_type_policy_map": {
                    "pydantic_ai.models.openrouter.OpenRouterModel": "image-only"
                },
            }
        ),
        encoding="utf-8",
    )

    resolver = media_tools.load_media_policy_resolver(config_path)
    policy = resolver.resolve(OpenRouterModel("custom/image-only"))

    assert policy.name == "image-only"
    assert policy.supported_media_types == frozenset({"image/png"})
    assert "openai" in resolver.policies
