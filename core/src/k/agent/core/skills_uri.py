"""`skills:` URI helpers.

We use a lightweight URI scheme `skills:<relative-path>` in prompts and skill
docs so references are portable across machines.

Resolution rules:
    - The `<relative-path>` portion is a filesystem-relative path (no leading
      slash) resolved under the skills root directory.
    - The skills root directory is `~/.kapybara/skills` at runtime.
    - In development/tests where we run against a checked-in filesystem tree
      rooted at `Config.fs_base` (e.g. `./data/fs`), the skills root is
      `<fs_base>/.kapybara/skills`.
    - `skills://...` (authority/netloc) is intentionally unsupported.

These helpers are intentionally small and dependency-free so they can be reused
by both prompt-generation and tool implementations.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

SKILLS_URI_SCHEME = "skills"


def skills_root_from_fs_base(fs_base: str | Path) -> Path:
    """Return the skills root under `fs_base`."""

    return Path(fs_base).expanduser().resolve() / ".kapybara" / "skills"


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

    rel = unquote(parsed.path)
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise ValueError("skills URIs must be relative paths")
    if any(part == ".." for part in rel_path.parts):
        raise ValueError("skills URIs must not contain '..' path traversal")

    return Path(skills_root).expanduser().resolve() / rel_path
