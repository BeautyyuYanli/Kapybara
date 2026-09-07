"""Bounded ZIP validation shared by storage and the CLI."""

import io
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from .types import InvalidSkill, SkillTooLarge

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_MEMBER = 32 * 1024 * 1024
MAX_MARKDOWN = 64 * 1024


@dataclass(frozen=True)
class Archive:
    name: str
    description: str
    skill_md: str
    root: str
    files: tuple[tuple[str, bytes, int], ...]


def _metadata(markdown: str) -> tuple[str, str]:
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        raise InvalidSkill("SKILL.md must start with YAML frontmatter")
    try:
        end = lines.index("---", 1)
        header = "\n".join(lines[1:end])
        if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(header)):
            raise InvalidSkill("YAML aliases are not allowed")
        data = yaml.safe_load(header)
    except (ValueError, yaml.YAMLError) as exc:
        raise InvalidSkill("Invalid SKILL.md frontmatter") from exc
    if not isinstance(data, dict):
        raise InvalidSkill("Skill frontmatter must be a mapping")
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise InvalidSkill("name must use lowercase letters, numbers, and single hyphens")
    if len(name) > 64:
        raise InvalidSkill("name exceeds 64 characters")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise InvalidSkill("description must be a nonempty string of at most 1024 characters")
    for key in ("license", "compatibility", "allowed-tools"):
        if key in data and not isinstance(data[key], str):
            raise InvalidSkill(f"{key} must be a string")
    if "compatibility" in data and len(data["compatibility"]) > 500:
        raise InvalidSkill("compatibility exceeds 500 characters")
    if "metadata" in data and (
        not isinstance(data["metadata"], dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in data["metadata"].items())
    ):
        raise InvalidSkill("metadata must map strings to strings")
    return name, description


def validate_archive(data: bytes) -> Archive:
    if len(data) > MAX_ARCHIVE:
        raise SkillTooLarge("ZIP exceeds 16 MiB")
    files: list[tuple[str, bytes, int]] = []
    paths: dict[str, bool] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ENTRIES:
                raise SkillTooLarge("ZIP exceeds 4096 entries")
            for member in members:
                raw = member.filename
                path = PurePosixPath(raw)
                directory = member.is_dir()
                if (
                    not raw
                    or "\\" in raw
                    or "\x00" in raw
                    or path.is_absolute()
                    or any(p in ("", ".", "..") for p in raw.rstrip("/").split("/"))
                    or ":" in path.parts[0]
                ):
                    raise InvalidSkill("Unsafe ZIP path")
                normalized = str(path)
                if normalized in paths:
                    raise InvalidSkill("Duplicate ZIP path")
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG):
                    raise InvalidSkill("ZIP contains a link or special file")
                if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                    raise InvalidSkill("Special permissions are not allowed")
                paths[normalized] = directory
                if member.file_size > MAX_MEMBER:
                    raise SkillTooLarge("ZIP member exceeds 32 MiB")
                if directory:
                    if member.file_size:
                        raise InvalidSkill("ZIP directory contains data")
                    continue
                total += member.file_size
                if total > MAX_EXPANDED:
                    raise SkillTooLarge("Expanded ZIP exceeds 128 MiB")
                with archive.open(member) as stream:
                    content = stream.read(MAX_MEMBER + 1)
                    if len(content) > MAX_MEMBER:
                        raise SkillTooLarge("ZIP member exceeds 32 MiB")
                    if len(content) != member.file_size:
                        raise InvalidSkill("ZIP size mismatch")
                files.append((normalized, content, 0o755 if mode & 0o111 else 0o644))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError) as exc:
        raise InvalidSkill("Invalid or unsupported ZIP archive") from exc
    for name in paths:
        if any(
            str(parent) in paths and not paths[str(parent)]
            for parent in PurePosixPath(name).parents
        ):
            raise InvalidSkill("Conflicting file and directory paths")
    markdowns = [
        (p, b)
        for p, b, _ in files
        if p == "SKILL.md"
        or (len(PurePosixPath(p).parts) == 2 and PurePosixPath(p).name == "SKILL.md")
    ]
    if len(markdowns) != 1:
        raise InvalidSkill("ZIP must contain one root SKILL.md")
    md_path, md_bytes = markdowns[0]
    if len(md_bytes) > MAX_MARKDOWN:
        raise SkillTooLarge("SKILL.md exceeds 64 KiB")
    try:
        markdown = md_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSkill("SKILL.md is not UTF-8") from exc
    name, description = _metadata(markdown)
    root = "" if md_path == "SKILL.md" else name
    if root and (
        md_path != f"{root}/SKILL.md"
        or any(p != root and not p.startswith(f"{root}/") for p in paths)
    ):
        raise InvalidSkill("ZIP root directory must match the skill name")
    return Archive(name, description, markdown, root, tuple(files))


def pack_skill(source_dir: Path, archive_path: Path) -> None:
    source = source_dir.absolute()
    target = archive_path.absolute()
    if source.is_symlink() or not source.is_dir():
        raise InvalidSkill("Source must be a regular directory")
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    if target.resolve().is_relative_to(source.resolve()):
        raise InvalidSkill("Archive must be outside the source directory")
    md_path = source / "SKILL.md"
    if md_path.is_symlink() or not md_path.is_file():
        raise InvalidSkill("Source has no regular SKILL.md")
    if md_path.stat().st_size > MAX_MARKDOWN:
        raise SkillTooLarge("SKILL.md exceeds 64 KiB")
    try:
        name, _ = _metadata(md_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise InvalidSkill("SKILL.md is not UTF-8") from exc
    buffer = io.BytesIO()
    count = total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for current, dirs, names in os.walk(source, followlinks=False):
            for entry in sorted(dirs + names):
                path = Path(current) / entry
                info = path.lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise InvalidSkill("Source contains a link or special file")
                if info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                    raise InvalidSkill("Source contains special permissions")
                count += 1
                if count > MAX_ENTRIES:
                    raise SkillTooLarge("Source exceeds 4096 entries")
                if stat.S_ISDIR(info.st_mode):
                    continue
                if info.st_size > MAX_MEMBER:
                    raise SkillTooLarge("Source member exceeds 32 MiB")
                total += info.st_size
                if total > MAX_EXPANDED:
                    raise SkillTooLarge("Source exceeds 128 MiB")
                zi = zipfile.ZipInfo(f"{name}/{path.relative_to(source).as_posix()}")
                zi.create_system = 3
                zi.external_attr = (stat.S_IFREG | (0o755 if info.st_mode & 0o111 else 0o644)) << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as file:
                    content = file.read(MAX_MEMBER + 1)
                if len(content) != info.st_size:
                    raise InvalidSkill("Source changed while packing")
                archive.writestr(zi, content)
                if buffer.tell() > MAX_ARCHIVE:
                    raise SkillTooLarge("ZIP exceeds 16 MiB")
    data = buffer.getvalue()
    validate_archive(data)
    try:
        with target.open("xb") as file:
            file.write(data)
    except BaseException:
        # Only remove a target created by this operation.
        raise


def extract_skill(archive_path: Path, destination: Path) -> Path:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    with archive_path.open("rb") as file:
        data = file.read(MAX_ARCHIVE + 1)
    archive = validate_archive(data)
    staging = Path(tempfile.mkdtemp(prefix=".kapy-skill-", dir=destination.parent))
    try:
        for name, content, mode in archive.files:
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as file:
                file.write(content)
            path.chmod(mode)
        # Reserve an empty destination, then publish the complete tree atomically.
        destination.mkdir()
        try:
            staging.rename(destination)
        except BaseException:
            destination.rmdir()
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination / archive.root if archive.root else destination
