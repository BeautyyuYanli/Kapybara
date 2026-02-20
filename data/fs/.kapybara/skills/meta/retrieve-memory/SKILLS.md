---
name: retrieve-memory
description: Works with the local ~/memories store.
---

# retrieve-memory

## What it is
- `~/memories` is a local memory store
- **core record files** (`~/memories/records/YYYY/MM/DD/HH/<id>.core.json`) that store one conversation memory each (one JSON object per line), including metadata and `compacted`.
- **detailed record files** (`~/memories/records/YYYY/MM/DD/HH/<id>.detailed.jsonl`) that store high-signal raw context as JSONL:
  - line 1: the raw `input` (JSON string)
  - line 2: the record `output` (JSON string)
  - line 3+: one line per `ModelResponse`, each line is a simplified tool-call list (JSON array)
- `*.detailed.jsonl` can still be verbose (raw `input` and `output` may be large).
  Prefer **partial reads** instead of loading whole files.
- `compacted` is the **working log** for the conversation: a chronological list of concise steps extracted from the agent’s tool traces (what was done, why, and the outcome).

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
# Search the detailed files (raw input + output + tool calls).
rg -n --sort path -g "*.detailed.jsonl" 'weather|天气|forecast' ~/memories/records | head -n 10
```

### Read only the beginning of a detailed file
```bash
# Just the first 2 lines: input, output
head -n 2 ~/memories/records/YYYY/MM/DD/HH/<id>.detailed.jsonl

# If needed, read a few response tool-call lines (each line is a JSON array)
sed -n '3,8p' ~/memories/records/YYYY/MM/DD/HH/<id>.detailed.jsonl
```

### Search compacted steps (the core files)
```bash
rg -n --sort path -g "*.core.json" 'ffmpeg|telegram|fish' ~/memories/records | head -n 10
```
