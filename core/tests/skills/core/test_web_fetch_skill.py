import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest


def _web_fetch_script_path() -> Path:
    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "core").is_dir() and (candidate / "data").is_dir():
            repo_root = candidate
            break
    else:
        raise RuntimeError(f"Could not locate repository root from {__file__!r}")
    return (
        repo_root
        / "data"
        / "fs"
        / ".kapybara"
        / "skills"
        / "core"
        / "web-fetch"
        / "fetch"
    )


def _load_web_fetch_module():
    script = _web_fetch_script_path()
    loader = SourceFileLoader("web_fetch_skill_script", str(script))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_prepare_output_dir_creates_unique_dir(tmp_path: Path) -> None:
    module = _load_web_fetch_module()
    module.TMP_ROOT = tmp_path.resolve()
    out_dir = tmp_path / "fetch_run_1"

    prepared = module._prepare_output_dir(out_dir=str(out_dir))

    assert prepared == out_dir.resolve()
    assert out_dir.is_dir()


def test_prepare_output_dir_rejects_non_tmp_output_dir(tmp_path: Path) -> None:
    module = _load_web_fetch_module()
    module.TMP_ROOT = tmp_path.resolve()

    with pytest.raises(ValueError, match="under /tmp"):
        module._prepare_output_dir(out_dir="/not-allowed")


def test_prepare_output_dir_rejects_existing_output_dir(tmp_path: Path) -> None:
    module = _load_web_fetch_module()
    module.TMP_ROOT = tmp_path.resolve()
    existing = tmp_path / "already_exists"
    existing.mkdir()

    with pytest.raises(ValueError, match="already exists"):
        module._prepare_output_dir(out_dir=str(existing))


def test_stdout_summary_outputs_full_run_metadata() -> None:
    module = _load_web_fetch_module()
    results = [
        {
            "index": index,
            "url": f"https://example.com/{index}",
            "status": "ok",
            "http_status": 200,
            "chars": 100 + index,
            "used_browser": False,
            "content_path": f"/tmp/content_{index}.txt",
            "error": None,
        }
        for index in range(3)
    ]
    manifest = {
        "generated_at": "2026-02-24T00:00:00Z",
        "output_dir": "/tmp/fetch_run",
        "content_dir": "/tmp/fetch_run/contents",
        "total_urls": len(results),
        "ok_count": len(results),
        "error_count": 0,
        "results": results,
    }

    summary = json.loads(module._stdout_summary(manifest))

    assert summary["output_dir"] == "/tmp/fetch_run"
    assert summary["content_dir"] == "/tmp/fetch_run/contents"
    assert summary["total_urls"] == len(results)
    assert len(summary["results"]) == 3


def test_build_parser_rejects_concurrency_option() -> None:
    module = _load_web_fetch_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "https://example.com",
                "--out-dir",
                "/tmp/fetch_case",
                "--concurrency",
                "64",
            ]
        )


def test_build_parser_rejects_timeout_option() -> None:
    module = _load_web_fetch_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["https://example.com", "--out-dir", "/tmp/fetch_case", "--timeout", "10"]
        )


def test_build_parser_rejects_out_option() -> None:
    module = _load_web_fetch_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "https://example.com",
                "--out-dir",
                "/tmp/fetch_case",
                "--out",
                "/tmp/fetch.json",
            ]
        )


def test_build_parser_requires_out_dir() -> None:
    module = _load_web_fetch_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["https://example.com"])
