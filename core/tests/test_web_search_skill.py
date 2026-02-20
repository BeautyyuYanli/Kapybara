import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


def _web_search_script_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
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
