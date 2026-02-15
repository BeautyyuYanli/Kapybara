from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import BinaryContent, ModelRetry
from pydantic_ai.messages import ImageUrl

from k.agent.core.agent import read_media


@pytest.mark.anyio
async def test_read_media_infers_url_kind_from_extension() -> None:
    ctx = SimpleNamespace()
    out = await read_media(ctx, ["https://example.com/a.jpg"])  # type: ignore[arg-type]

    assert len(out) == 1
    assert isinstance(out[0], ImageUrl)
    assert out[0].url == "https://example.com/a.jpg"


@pytest.mark.anyio
async def test_read_media_rejects_kind_prefixes() -> None:
    ctx = SimpleNamespace()
    with pytest.raises(ModelRetry):
        await read_media(ctx, ["audio:https://example.com/stream"])  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_read_media_expands_env_vars_for_local_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.bin").write_bytes(b"hello")
    monkeypatch.setenv("K_TEST_MEDIA_DIR", str(tmp_path))

    ctx = SimpleNamespace()
    out = await read_media(ctx, ["$K_TEST_MEDIA_DIR/x.bin"])  # type: ignore[arg-type]

    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)


@pytest.mark.anyio
async def test_read_media_rejects_empty_strings() -> None:
    ctx = SimpleNamespace()
    with pytest.raises(ModelRetry):
        await read_media(ctx, ["  "])  # type: ignore[arg-type]
