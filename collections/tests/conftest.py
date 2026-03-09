"""Pytest setup for collection tests that exercise shared core helpers."""

from __future__ import annotations

import sys
from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[2] / "core" / "src"

if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))
