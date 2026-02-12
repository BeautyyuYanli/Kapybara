---
name: retrieve-memory
description: Works with the local ~/memories store (index + JSON records).
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

    raw_pair: tuple[str, str]
    compacted: list[str]
    detailed: list[ModelRequest | ModelResponse]
```

## IDs
 base64url of **48-bit** big-endian `created_at` **POSIX millis** (usually ~8 chars)

## Common tasks
Combined with `core/file-search` skill for searching.

```bash
# search by keywords in records
# sort in reverse path order to get newest first
 rg -n --sortr path -g "*.json" 'weather|天气|forecast' memories/records | head -n 20

# open a record
# detailed content is usually too verbose
cat ~/memories/records/YYYY/MM/DD/HH/<id>.json | jq -M 'del(.detailed)'
```
