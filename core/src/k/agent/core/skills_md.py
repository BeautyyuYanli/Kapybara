"""Skills prompt helpers.

The agent injects skills documentation (SKILLS.md) into the system prompt so
the model can discover available workflows. This module keeps filesystem
reading logic separate from the agent wiring.
"""

from __future__ import annotations

from pathlib import Path

from k.agent.channels import channel_root
from k.agent.core.skills_uri import skills_root_from_fs_base, skills_uri


def concat_skills_md(base_path: str | Path) -> str:
    """Scan `<base_path>/.kapybara/skills/{core,meta}/*/SKILLS.md` and concatenate.

    Returns a single string which is the concatenation of all found SKILLS.md files,
    separated by clear delimiters.
    """

    skills_root = skills_root_from_fs_base(base_path)

    chunks: list[str] = []

    for group in ("core", "meta"):
        group_root = skills_root / group
        if not group_root.exists():
            continue

        for md in sorted(
            group_root.glob("*/SKILLS.md"), key=lambda p: (p.parent.name, str(p))
        ):
            content = md.read_text()
            chunks.append(
                "\n".join(
                    [
                        f"# ===== {skills_uri(f'{group}/{md.parent.name}/SKILLS.md')} =====",
                        content.rstrip(),
                        "",
                    ]
                )
            )

    return "\n".join(chunks).rstrip() + "\n"


def maybe_load_channel_skill_md(
    base_path: str | Path,
    *,
    group: str,
    channel: str | None,
) -> str | None:
    """Load `skills:<group>/<channel_root>/SKILLS.md` if present.

    The agent uses `context/<platform>` and `messager/<platform>` skills where
    `<platform>` is the first segment of an input/output channel path.
    """

    if not channel:
        return None

    root = channel_root(channel)
    md = skills_root_from_fs_base(base_path) / group / root / "SKILLS.md"
    if not md.exists():
        return None

    content = md.read_text()
    return "\n".join(
        [
            f"# ===== {skills_uri(f'{group}/{root}/SKILLS.md')} =====",
            content.rstrip(),
            "",
        ]
    )
