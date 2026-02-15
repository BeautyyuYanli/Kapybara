#!/usr/bin/env python3
"""CLI wrapper for migrating folder-backed memory records to split core/detailed files.

Run from the repo:
  cd core
  pdm run python scripts/migrate_folder_memory_records_split.py --dry-run
"""

from __future__ import annotations

from k.agent.memory.migrate_split import main

if __name__ == "__main__":
    raise SystemExit(main())
