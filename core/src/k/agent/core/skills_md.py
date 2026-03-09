"""Skills prompt assembly and `skills:` URI helpers.

The agent injects skills documentation (`SKILLS.md`) into the system prompt so
the model can discover available workflows. This module owns both filesystem
discovery for prompt injection and the lightweight `skills:` URI scheme used
to label embedded skill docs.

Resolution rules:
    - `skills:<relative-path>` resolves under `<config_base>/skills`.
    - The `<relative-path>` portion must remain relative and must not traverse
      outside the skills root.
    - `skills://...` (authority/netloc) is intentionally unsupported.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from k.agent.channels import channel_root

SKILLS_URI_SCHEME = "skills"


def skills_root_from_config_base(config_base: str | Path) -> Path:
    """Return the skills root under `config_base`."""

    return Path(config_base).expanduser().resolve() / "skills"


def skills_uri(relative_path: str | Path) -> str:
    """Format a `skills:` URI for a path relative to the skills root."""

    rel = Path(relative_path).as_posix().lstrip("/")
    return f"{SKILLS_URI_SCHEME}:{rel}"


def resolve_skills_uri(uri: str, *, skills_root: str | Path) -> Path:
    """Resolve a `skills:` URI into an absolute path under `skills_root`.

    This is purely a resolver; the returned path may or may not exist.

    Raises:
        ValueError: If the URI scheme is not `skills:`, if it uses an authority
            component, if it's absolute, or if it attempts path traversal.
    """

    parsed = urlparse(uri)
    if parsed.scheme != SKILLS_URI_SCHEME:
        raise ValueError(f"Unsupported scheme for skills URI: {parsed.scheme!r}")
    if parsed.netloc:
        raise ValueError("skills URIs must not include an authority component")

    rel_path = Path(unquote(parsed.path))
    if rel_path.is_absolute():
        raise ValueError("skills URIs must be relative paths")
    if any(part == ".." for part in rel_path.parts):
        raise ValueError("skills URIs must not contain '..' path traversal")

    return Path(skills_root).expanduser().resolve() / rel_path


def _format_skill_md_chunk(relative_path: str | Path, content: str) -> str:
    """Wrap SKILLS.md content in the delimiter format expected by prompts."""

    return "\n".join(
        [
            f"# ===== {skills_uri(relative_path)} =====",
            content.rstrip(),
            "",
        ]
    )


def concat_skills_md(config_base: str | Path) -> str:
    """Scan `<config_base>/skills/{core,meta}/*/SKILLS.md` and concatenate.

    Returns a single string which is the concatenation of all found SKILLS.md files,
    separated by clear delimiters.
    """

    skills_root = skills_root_from_config_base(config_base)

    chunks: list[str] = []

    for group in ("core", "meta"):
        group_root = skills_root / group
        if not group_root.exists():
            continue

        for md in sorted(
            group_root.glob("*/SKILLS.md"), key=lambda p: (p.parent.name, str(p))
        ):
            chunks.append(
                _format_skill_md_chunk(
                    f"{group}/{md.parent.name}/SKILLS.md",
                    md.read_text(),
                )
            )

    return "\n".join(chunks).rstrip() + "\n"


def maybe_load_channel_skill_md(
    config_base: str | Path,
    *,
    group: str,
    channel: str | None,
) -> str | None:
    """Load `skills:<group>/<channel_root>/SKILLS.md` if present.

    Channel-root groups (for example `messager/<platform>`) use the first
    segment of the channel path as `<platform>`.
    """

    if not channel:
        return None

    root = channel_root(channel)
    md = skills_root_from_config_base(config_base) / group / root / "SKILLS.md"
    if not md.exists():
        return None

    return _format_skill_md_chunk(
        f"{group}/{root}/SKILLS.md",
        md.read_text(),
    )
