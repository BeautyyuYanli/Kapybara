---
name: retrieve-memory
description: Works with the local ~/memories store (index + JSON records).
---

# retrieve-memory

## What it is
`~/memories` is a local memory store:
- an **append-only index** (`~/memories/order.jsonl`) that points to
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
Combined with `core/file_search` skill for searching.

```bash
# newest entries
 tail -n 20 ~/memories/order.jsonl

# open a record
 jq . ~/memories/records/YYYY/MM/DD/HH/<id>.json

# search
 rg -n 'weather|天气|forecast' ~/memories/records -g '*.json'
```
