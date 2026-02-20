"""Skills prompt helpers.

The agent injects skills documentation (SKILLS.md) into the system prompt so
the model can discover available workflows. This module keeps filesystem
reading logic separate from the agent wiring.
"""

from __future__ import annotations

from pathlib import Path

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


def maybe_load_kind_skill_md(
    base_path: str | Path,
    *,
    group: str,
    kind: str | None,
) -> str | None:
    """Load `skills:<group>/<kind>/SKILLS.md` if present.

    The agent uses `context/<kind>` and `messager/<kind>` skills to route and
    contextualize replies for structured `Event` inputs. We only inject the
    specific kind's skills (instead of all `context/*` / `messager/*`) to keep
    the system prompt compact.
    """

    if not kind:
        return None

    md = skills_root_from_fs_base(base_path) / group / kind / "SKILLS.md"
    if not md.exists():
        return None

    content = md.read_text()
    return "\n".join(
        [
            f"# ===== {skills_uri(f'{group}/{kind}/SKILLS.md')} =====",
            content.rstrip(),
            "",
        ]
    )
