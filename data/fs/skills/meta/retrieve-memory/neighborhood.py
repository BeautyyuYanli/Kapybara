#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

"""Print a memory neighborhood (related records) from the local ~/memories store.

A record’s “neighborhood” is all records reachable from a starting record by
following `parents` and/or `children` links up to N steps (inclusive), plus the
starting record itself.

- Traverses both directions (`parents` and `children`).
- Uses a single level bound (max BFS depth).
- Prints results as NDJSON.
- Sorts results by id (which is chronological for this id scheme).

Example output line:
  {"id_":"AZxS4r6O","parents":[],"children":["AZxS5E8z"],...}
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import deque
from typing import Any


def build_index(root: str) -> dict[str, str]:
    index: dict[str, str] = {}
    pat = os.path.join(os.path.expanduser(root), "**/*.json")
    for p in glob.glob(pat, recursive=True):
        try:
            with open(p, "r", encoding="utf-8") as f:
                j = json.load(f)
            mid = j.get("id_") or j.get("id")
            if mid:
                index[str(mid)] = p
        except Exception:
            continue
    return index


def load_record(index: dict[str, str], mid: str) -> dict[str, Any] | None:
    p = index.get(mid)
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def related(index: dict[str, str], start: str, max_depth: int) -> dict[str, int]:
    """Return {id: depth} for all nodes reachable within max_depth (inclusive)."""

    depths: dict[str, int] = {start: 0}
    q: deque[str] = deque([start])

    while q:
        cur = q.popleft()
        d = depths[cur]
        if d == max_depth:
            continue

        rec = load_record(index, cur)
        if not rec:
            continue

        for edge in ("parents", "children"):
            for nxt in rec.get(edge, []) or []:
                nxt = str(nxt)
                nd = d + 1
                if nxt in depths and depths[nxt] <= nd:
                    continue
                if nd > max_depth:
                    continue
                depths[nxt] = nd
                q.append(nxt)

    return depths


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Print a memory neighborhood: the base id plus all ancestors/descendants within N levels, sorted by id (chronological)."
        )
    )
    ap.add_argument("id", help="Base memory id (e.g. AZxSw9mW).")
    ap.add_argument(
        "-n",
        "--levels",
        type=int,
        default=3,
        help="Max BFS depth (levels) to expand in both directions.",
    )
    args = ap.parse_args()

    index = build_index("~/memories/records")
    depths = related(index, args.id, args.levels)

    for mid in sorted(depths.keys()):
        rec = load_record(index, mid)
        if rec is None:
            continue
        print(json.dumps(rec, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
