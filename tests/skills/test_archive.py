import io
import stat
import zipfile
from pathlib import Path

import pytest

from kapy.skills import InvalidSkill, SkillTooLarge, extract_skill, pack_skill
from kapy.skills.archive import MAX_ARCHIVE, MAX_ENTRIES, MAX_MARKDOWN, MAX_MEMBER, validate_archive


def archive(name: str = "demo", description: str = "A useful skill") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as file:
        file.writestr("SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\nBody\n")
        file.writestr("scripts/run.sh", "echo hello\n")
    return buffer.getvalue()


def test_pack_extract_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: useful\n---\nBody\n")
    (source / "run").write_text("#!/bin/sh\necho hello\n")
    (source / "run").chmod(0o755)
    packed = tmp_path / "archive.zip"
    pack_skill(source, packed)
    root = extract_skill(packed, tmp_path / "out")
    assert root.name == "demo"
    assert (root / "run").read_bytes() == (source / "run").read_bytes()
    assert (root / "run").stat().st_mode & 0o111
    with pytest.raises(FileExistsError):
        extract_skill(packed, tmp_path / "out")


@pytest.mark.parametrize("path", ["../escape", "/absolute", "demo/../escape", "a\\b"])
def test_reject_traversal(path: str) -> None:
    buffer = io.BytesIO(archive())
    with zipfile.ZipFile(buffer, "a") as file:
        file.writestr(path, "bad")
    with pytest.raises(InvalidSkill):
        validate_archive(buffer.getvalue())


def test_reject_link_and_yaml_alias() -> None:
    buffer = io.BytesIO(archive())
    with zipfile.ZipFile(buffer, "a") as file:
        member = zipfile.ZipInfo("link")
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        file.writestr(member, "/etc/passwd")
    with pytest.raises(InvalidSkill):
        validate_archive(buffer.getvalue())
    with pytest.raises(InvalidSkill):
        validate_archive(archive(description="&x alias\nmetadata: {x: *x}"))


def test_source_symlink_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: useful\n---\n")
    (source / "link").symlink_to("/etc/passwd")
    with pytest.raises(InvalidSkill):
        pack_skill(source, tmp_path / "archive.zip")
    assert not (tmp_path / "archive.zip").exists()


@pytest.mark.parametrize("resource", ["zip", "member", "markdown", "entries"])
def test_archive_resource_limits(resource: str) -> None:
    if resource == "zip":
        data = b"x" * (MAX_ARCHIVE + 1)
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as file:
            file.writestr(
                "SKILL.md",
                "x" * (MAX_MARKDOWN + 1)
                if resource == "markdown"
                else "---\nname: demo\ndescription: useful\n---\n",
            )
            if resource == "member":
                file.writestr("large", b"x" * (MAX_MEMBER + 1))
            if resource == "entries":
                for index in range(MAX_ENTRIES):
                    file.writestr(f"entry-{index}", b"")
        data = buffer.getvalue()
    with pytest.raises(SkillTooLarge):
        validate_archive(data)


@pytest.mark.parametrize("path", ["scripts/run.sh", "scripts"])
def test_duplicate_and_file_directory_conflicts(path: str) -> None:
    buffer = io.BytesIO(archive())
    with zipfile.ZipFile(buffer, "a") as file:
        if path == "scripts/run.sh":
            with pytest.warns(UserWarning, match="Duplicate name"):
                file.writestr(path, "duplicate")
        else:
            file.writestr(path, "conflicts with scripts/run.sh")
    with pytest.raises(InvalidSkill, match="Duplicate|Conflicting"):
        validate_archive(buffer.getvalue())
