import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest


def _web_search_script_path() -> Path:
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
        / "web-search"
        / "search"
    )


def _load_web_search_module():
    script = _web_search_script_path()
    loader = SourceFileLoader("web_search_skill_script", str(script))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_format_search_output_keeps_only_compact_fields(tmp_path: Path) -> None:
    module = _load_web_search_module()
    out_path = tmp_path / "search.json"
    full_text_dir = tmp_path / "search.json.full_text"

    raw_payload = json.dumps(
        {
            "code": 200,
            "status": 20000,
            "data": [
                {
                    "title": "One",
                    "url": "https://example.com/one",
                    "description": "short description",
                    "content": "long snippet " * 80,
                    "score": 0.998,
                    "metadata": {"foo": "bar"},
                }
            ],
        }
    )

    output = json.loads(
        module._format_search_output(raw_payload, "example query", str(out_path))
    )
    assert output["query"] == "example query"
    assert output["output_path"] == str(out_path.resolve())
    assert output["full_text_dir"] == str(full_text_dir.resolve())
    assert output["upstream"]["code"] == 200
    assert output["upstream"]["status"] == 20000
    assert len(output["results"]) == 1

    result = output["results"][0]
    assert set(result).issubset(
        {"title", "url", "description", "snippet", "full_text_path"}
    )
    assert result["title"] == "One"
    assert result["url"] == "https://example.com/one"
    assert result["description"] == "short description"
    assert "snippet" in result
    assert "full_text_path" in result
    assert len(result["snippet"]) <= module.MAX_SNIPPET_CHARS
    assert "score" not in result
    assert "metadata" not in result

    full_text_path = Path(result["full_text_path"])
    assert full_text_path.is_file()
    assert full_text_path.parent == full_text_dir.resolve()
    assert full_text_path.read_text(encoding="utf-8").strip().startswith("long snippet")


def test_format_search_output_surfaces_upstream_error(tmp_path: Path) -> None:
    module = _load_web_search_module()
    out_path = tmp_path / "search_error.json"

    raw_payload = json.dumps(
        {
            "code": 401,
            "status": 40103,
            "name": "AuthenticationRequiredError",
            "message": "Authentication is required to use this endpoint.",
            "data": None,
        }
    )

    output = json.loads(
        module._format_search_output(raw_payload, "error query", str(out_path))
    )
    assert output["query"] == "error query"
    assert output["output_path"] == str(out_path.resolve())
    assert output["results"] == []
    assert output["upstream"]["code"] == 401
    assert output["upstream"]["status"] == 40103
    assert output["upstream"]["name"] == "AuthenticationRequiredError"
    assert output["error"] == "Authentication is required to use this endpoint."


def test_format_search_output_non_json_uses_truncated_preview(tmp_path: Path) -> None:
    module = _load_web_search_module()
    out_path = tmp_path / "search_broken.json"

    non_json_payload = "<html>" + ("x" * 600)
    output = json.loads(
        module._format_search_output(non_json_payload, "broken query", str(out_path))
    )

    assert output["query"] == "broken query"
    assert output["output_path"] == str(out_path.resolve())
    assert output["full_text_dir"] == str(
        (tmp_path / "search_broken.json.full_text").resolve()
    )
    assert output["results"] == []
    assert output["error"] == "Search response was not valid JSON."
    assert len(output["raw_preview"]) <= module.MAX_SNIPPET_CHARS


def test_format_search_output_non_object_json_uses_error(tmp_path: Path) -> None:
    module = _load_web_search_module()
    out_path = tmp_path / "search_list_root.json"

    payload = json.dumps([{"title": "not expected root"}])
    output = json.loads(
        module._format_search_output(payload, "list root", str(out_path))
    )

    assert output["query"] == "list root"
    assert output["results"] == []
    assert output["error"] == "Search response JSON root was not an object."


def test_build_jobs_supports_multiple_queries_under_tmp(tmp_path: Path) -> None:
    module = _load_web_search_module()
    module.TMP_ROOT = tmp_path.resolve()
    out_dir = tmp_path / "search_run_1"

    jobs = module._build_jobs(["q1", "q2"], out_dir=str(out_dir))

    assert len(jobs) == 2
    assert jobs[0].query == "q1"
    assert jobs[1].query == "q2"
    assert str(jobs[0].out_path).startswith(str(out_dir.resolve()))
    assert str(jobs[1].out_path).startswith(str(out_dir.resolve()))
    assert jobs[0].out_path != jobs[1].out_path
    assert out_dir.is_dir()


def test_build_jobs_rejects_non_tmp_output_dir(tmp_path: Path) -> None:
    module = _load_web_search_module()
    module.TMP_ROOT = tmp_path.resolve()

    with pytest.raises(ValueError, match="under /tmp"):
        module._build_jobs(["query"], out_dir="/not-allowed")


def test_build_jobs_rejects_existing_output_dir(tmp_path: Path) -> None:
    module = _load_web_search_module()
    module.TMP_ROOT = tmp_path.resolve()
    existing = tmp_path / "already_exists"
    existing.mkdir()

    with pytest.raises(ValueError, match="already exists"):
        module._build_jobs(["query"], out_dir=str(existing))


def test_build_parser_rejects_concurrency_option() -> None:
    module = _load_web_search_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["example query", "--out-dir", "/tmp/search_case", "--concurrency", "64"]
        )


def test_build_parser_rejects_timeout_option() -> None:
    module = _load_web_search_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["example query", "--out-dir", "/tmp/search_case", "--timeout", "10"]
        )


def test_build_parser_rejects_out_option() -> None:
    module = _load_web_search_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "example query",
                "--out-dir",
                "/tmp/search_case",
                "--out",
                "/tmp/search.json",
            ]
        )


def test_build_parser_requires_out_dir() -> None:
    module = _load_web_search_module()
    parser = module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["example query"])


def test_summary_row_includes_all_results_with_description(tmp_path: Path) -> None:
    module = _load_web_search_module()
    out_path = tmp_path / "search_summary.json"

    raw_payload = json.dumps(
        {
            "code": 200,
            "status": 20000,
            "data": [
                {
                    "title": "One",
                    "url": "https://example.com/one",
                    "description": "desc one",
                },
                {
                    "title": "Two",
                    "url": "https://example.com/two",
                    "description": "x" * 500,
                },
                {
                    "title": "Three",
                    "url": "https://example.com/three",
                    "description": "desc three",
                },
            ],
        }
    )

    payload = module.SearchOutputPayload.model_validate_json(
        module._format_search_output(raw_payload, "summary query", str(out_path))
    )
    row = module._summary_row(payload)

    assert row.results is not None
    assert len(row.results) == 3
    assert row.results[0]["description"] == "desc one"
    assert len(row.results[1]["description"]) <= module.MAX_STDOUT_DESCRIPTION_CHARS


def test_stdout_summary_uses_results_key_for_all_query_rows(tmp_path: Path) -> None:
    module = _load_web_search_module()
    rows = [
        module.SearchSummaryRow(
            query="q1",
            result_count=1,
            output_path="/tmp/q1.json",
            results=[
                {
                    "title": "t1",
                    "url": "https://example.com/1",
                    "description": "d1",
                }
            ],
        ),
        module.SearchSummaryRow(
            query="q2",
            result_count=1,
            output_path="/tmp/q2.json",
            results=[
                {
                    "title": "t2",
                    "url": "https://example.com/2",
                    "description": "d2",
                }
            ],
        ),
    ]

    summary = json.loads(module._stdout_summary(rows))

    assert summary["total_queries"] == 2
    assert len(summary["results"]) == 2
    assert "results_preview" not in summary
    assert "manifest_path" not in summary
