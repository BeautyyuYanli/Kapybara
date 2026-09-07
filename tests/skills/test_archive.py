import io
import stat
import zipfile
from pathlib import Path

import pytest

from kapy.skills import InvalidSkill, extract_skill, pack_skill
from kapy.skills.archive import validate_archive


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
