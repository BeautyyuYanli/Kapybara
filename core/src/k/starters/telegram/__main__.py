"""`python -m k.starters.telegram` entrypoint.

Keep behavior identical to the historical single-file module by delegating to
`k.starters.telegram.main`.
"""

from __future__ import annotations

import anyio

from . import main

if __name__ == "__main__":
    anyio.run(main)
