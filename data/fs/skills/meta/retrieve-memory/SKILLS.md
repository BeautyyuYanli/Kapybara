---
name: retrieve-memory
description: Works with the local ~/memories store.
---

# retrieve-memory

## What it is
- `~/memories` is a local memory store
- **record files** (`~/memories/records/YYYY/MM/DD/HH/<id>.json`) that store one conversation memory each.

A record is defined as:
```
class MemoryRecord(BaseModel):
    created_at: datetime
    id_: str
    parents: list[str]
    children: list[str]

    input: str
    compacted: list[str]
    output: str
```

## IDs
An **8-character**, URL-safe encoding of a **48-bit** big-endian
POSIX-milliseconds timestamp (`created_at`), using a custom alphabet whose ASCII order matches digit values (so lexicographic order matches time order).

## Common tasks
Combined with `core/file-search` skill for searching.

### Search by keywords
```bash
# sort in reverse path order to get newest first
rg -n --sortr path -g "*.json" 'weather|天气|forecast' ~/memories/records | head -n 10
```

### Get memory neighborhood (related records via parents/children) up to N levels
The `parents` / `children` fields form a graph that links related memory records.

Here, a record’s “neighborhood” means: **all records reachable from a starting record by following `parents` and/or `children` links up to N steps** (inclusive), plus the starting record itself.

- `parents`: IDs of the record(s) this memory was derived from / depends on (e.g., the earlier memory you continued from, summarized, or referenced).
- `children`: IDs of records that were created later and explicitly link back to this record (e.g., follow-ups, refinements, or downstream summaries).

This helper traverses **both directions** (ancestors + descendants) via BFS, up to depth `N`.

Sidecar script:
- `~/skills/meta/retrieve-memory/neighborhood.py` (PEP 723; runnable directly)

```bash
id='<memory_id>'
N=3
~/skills/meta/retrieve-memory/neighborhood.py "$id" --levels "$N"
```

Notes:
- Output format is NDJSON (one JSON record per line).
- Results are sorted by chronological order (older → newer).
