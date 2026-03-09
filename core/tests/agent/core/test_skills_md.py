from pathlib import Path

import pytest

from k.agent.core.skills_md import (
    concat_skills_md,
    resolve_skills_uri,
    skills_root_from_config_base,
    skills_uri,
)


def _write_skill(config_base: Path, *, group: str, name: str, content: str) -> None:
    path = config_base / "skills" / group / name / "SKILLS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_skills_root_from_config_base_appends_skills_dir(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"
    assert skills_root_from_config_base(config_base) == config_base.resolve() / "skills"


def test_skills_uri_normalizes_relative_paths() -> None:
    assert (
        skills_uri("/core/web-search/SKILLS.md") == "skills:core/web-search/SKILLS.md"
    )


def test_resolve_skills_uri_decodes_path_under_skills_root(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    resolved = resolve_skills_uri(
        "skills:core/my%20skill/SKILLS.md",
        skills_root=skills_root,
    )

    assert resolved == skills_root.resolve() / "core" / "my skill" / "SKILLS.md"


@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("http:core/web-search/SKILLS.md", "Unsupported scheme"),
        ("skills://core/web-search/SKILLS.md", "authority component"),
        ("skills:/core/web-search/SKILLS.md", "relative paths"),
        ("skills:../web-search/SKILLS.md", "path traversal"),
    ],
)
def test_resolve_skills_uri_rejects_invalid_uris(uri: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_skills_uri(uri, skills_root="/tmp/skills")


def test_concat_skills_md_uses_skills_uri_headers(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"
    _write_skill(config_base, group="core", name="web-search", content="core skill")
    _write_skill(
        config_base, group="meta", name="retrieve-memory", content="meta skill"
    )

    result = concat_skills_md(config_base)

    assert result == (
        "# ===== skills:core/web-search/SKILLS.md =====\n"
        "core skill\n\n"
        "# ===== skills:meta/retrieve-memory/SKILLS.md =====\n"
        "meta skill\n"
    )
