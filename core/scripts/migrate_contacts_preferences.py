#!/usr/bin/env python3
"""Migrate contact preference files to unique-id-backed symlink layout.

Target layout under `<config_base>/preferences/contacts`:
- `data/<unique_id>.md`: canonical preference content
- `<platform>/<platform_id>.md`: relative symlink to the canonical file

The unique-id mapping is persisted in `<config_base>/contacts.json` as:
`dict[str, list[str]]`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ensure_src_on_sys_path() -> None:
    """Make `core/src` importable when this script is run from a checkout."""

    core_src = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(core_src))


_ensure_src_on_sys_path()


def main() -> int:
    from k.agent.contacts import migrate_contacts_preferences

    parser = argparse.ArgumentParser(
        description=(
            "Migrate preferences/contacts/<platform>/<platform_id>.md files to "
            "relative symlinks targeting preferences/contacts/data/<unique_id>.md, "
            "and maintain contacts.json."
        )
    )
    parser.add_argument(
        "--config-base",
        type=Path,
        default=Path("~/.kapybara"),
        help="Path to the .kapybara directory (default: ~/.kapybara).",
    )
    args = parser.parse_args()

    config_base = args.config_base.expanduser().resolve()
    report = migrate_contacts_preferences(config_base=config_base)
    print(
        "\n".join(
            [
                f"config_base: {config_base}",
                f"created_contact_ids: {report.created_contact_ids}",
                f"created_data_files: {report.created_data_files}",
                f"rewritten_symlinks: {report.rewritten_symlinks}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
