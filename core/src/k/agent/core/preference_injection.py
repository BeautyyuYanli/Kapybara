"""Preference prompt loading for `k.agent.core.agent`.

This module isolates channel/contact preference discovery so `agent.py`
only wires the resulting prompt into the system prompt chain.
"""

from __future__ import annotations

from pathlib import Path

from k.agent.channels import effective_out_channel, iter_channel_prefixes
from k.agent.contacts import contact_preference_path


def _root_preference_candidates(pref_root: Path) -> list[Path]:
    """Build root-level preference candidate file paths in priority order.

    If `PREFERENCES.md` exists, do not include `PREFERENCES.default.md`.
    """

    preferred = pref_root / "PREFERENCES.md"
    default = pref_root / "PREFERENCES.default.md"
    if preferred.exists():
        return [preferred]
    return [default]


def _channel_preference_candidates(
    in_channel: str,
    *,
    out_channel: str | None = None,
    pref_root: Path,
) -> list[Path]:
    """Build preference candidate paths in deterministic load order.

    Order:
    1. Root-level preference (`PREFERENCES.md` or fallback `PREFERENCES.default.md`)
    2. `in_channel` prefixes from root to leaf:
       - `<prefix>.md`
       - `<prefix>/PREFERENCES.md`
    3. Effective `out_channel` prefixes from root to leaf (deduped against the
       previously added paths):
       - `<prefix>.md`
       - `<prefix>/PREFERENCES.md`
    """

    out: list[Path] = _root_preference_candidates(pref_root)
    seen: set[Path] = set(out)
    routed_channels = [in_channel]
    resolved_out_channel = effective_out_channel(
        in_channel=in_channel,
        out_channel=out_channel,
    )
    if resolved_out_channel != in_channel:
        routed_channels.append(resolved_out_channel)

    for channel in routed_channels:
        for prefix in iter_channel_prefixes(channel):
            for candidate in (
                pref_root / f"{prefix}.md",
                pref_root / prefix / "PREFERENCES.md",
            ):
                if candidate in seen:
                    continue
                seen.add(candidate)
                out.append(candidate)
    return out


def _contact_preference_candidates(
    *, contacts: list[str], pref_root: Path
) -> list[Path]:
    """Return user-level preference paths for `<platform>/<user_id>` contacts."""

    out: list[Path] = []
    seen: set[Path] = set()
    for contact in contacts:
        path = contact_preference_path(
            pref_root=pref_root,
            platform_contact=contact,
        )
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def load_preferences_prompt(
    *,
    in_channel: str,
    contacts: list[str],
    pref_root: Path,
    out_channel: str | None = None,
) -> str:
    """Load root-level, routed-channel, and contact-level preferences.

    Preference files are resolved under `pref_root` (normally
    `<config_base>/preferences`).

    Returns:
        Empty string when no preference files match; otherwise a
        `<Preferences>...</Preferences>` block with one section per loaded file.
        Each section starts with its corresponding preference path. For
        symlinks, the path header also shows the resolved absolute target path.
    """

    candidates = _channel_preference_candidates(
        in_channel,
        out_channel=out_channel,
        pref_root=pref_root,
    )
    candidates.extend(
        _contact_preference_candidates(contacts=contacts, pref_root=pref_root)
    )

    blocks: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        blocks.append(
            "\n".join([f"Path: {_display_preference_path(path)}", text, "---"])
        )

    if not blocks:
        return ""
    comment = "**The following are your preferences, written in your first person.**"
    return (
        f"<Preferences>\n{comment}\n" + "\n".join(blocks).rstrip() + "\n</Preferences>"
    )


def _display_preference_path(path: Path) -> str:
    """Return display text for preference path headers in system prompts.

    Normal files are shown as-is. Symlinks show both the symlink path and the
    resolved absolute target (`<path> -> <resolved_abs_target>`).
    """

    if not path.is_symlink():
        return str(path)
    return f"{path} -> {path.resolve()}"
