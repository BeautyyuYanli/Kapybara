---
name: retrieve-memory
description: Works with the local ~/.kapybara/memories store.
---

# retrieve-memory

## Recommended script (Stage A)
Use this first for channel-scoped keyword retrieval:

```bash
~/.kapybara/skills/meta/retrieve-memory/stage_a \
  --in-channel <exact_current_input_in_channel> \
  --kw <regex> \
  [--n <N>] \
  --out /tmp/mem_ctx_<unique>.tsv
```

### Stage A contract
- `--in-channel` must be the exact current input channel string.
- Retrieval is scoped to that channel subtree (`prefix` semantics).
- `--kw` is a regex matched against detailed memory lines.
- Output TSV columns: `id`, `core_json`, `matched_detailed_lines`.
- `matched_detailed_lines` is a JSON array of `{line, text}` matches.
- Always use a unique `--out` path to avoid races/clobbering.

## Manual fallback (lower-level)
Use manual commands when Stage A is insufficient or when you need custom filtering.

### Search detailed files by keyword
```bash
rg -n --sort path -g "*.detailed.jsonl" 'weather|forecast' ~/.kapybara/memories/records | head -n 10
```

### Read only the beginning of a detailed file
```bash
head -n 2 ~/.kapybara/memories/records/YYYY/MM/DD/HH/<id>.detailed.jsonl
sed -n '3,8p' ~/.kapybara/memories/records/YYYY/MM/DD/HH/<id>.detailed.jsonl
```

### Search compacted steps in core files
```bash
rg -n --sort path -g "*.core.json" 'ffmpeg|telegram|fish' ~/.kapybara/memories/records | head -n 10
```

## What this skill reads
- `~/.kapybara/memories` is the local memory store.
- **core record files** (`~/.kapybara/memories/records/YYYY/MM/DD/HH/<id>.core.json`) store metadata and `compacted`.
- **detailed record files** (`~/.kapybara/memories/records/YYYY/MM/DD/HH/<id>.detailed.jsonl`) store JSONL:
  - line 1: raw `input` (JSON string)
  - line 2: record `output` (JSON string)
  - line 3+: one simplified tool-call list per `ModelResponse` (JSON array)
- `*.detailed.jsonl` can still be verbose; prefer partial reads.
- `compacted` is the working log: concise chronological steps from tool traces.

A record is defined as:
```
class MemoryRecord(BaseModel):
    created_at: datetime
    in_channel: str
    out_channel: str | None
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
