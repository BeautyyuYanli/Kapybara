---
name: context/telegram
description: Optimizes Telegram context/memory retrieval for speed and accuracy without building indexes.
---

# context/telegram

This skill is a lightweight, **no-index** workflow for retrieving relevant context from the local `~/memories/records` store when replying on Telegram.

The recommended entrypoint is the bundled **Stage A** script, which does a parallel candidate search by:
- `chat.id`
- `from.id`
- optional keyword regex (scoped to the same chat)

## Recommended: Stage A script

### Usage

```bash
~/skills/context/telegram/stage_a \
  --chat-id <chat_id> \
  [--from-id <from_id>] \
  [--kw <regex>] \
  [--root <dir>] \
  [--n <N>] \
  [--out <dir>]
```

Notes:
- The script forces ripgrep output to **not** include line numbers.
- `--n` controls how many lines are kept per route (chat / user / kw). Default: `7`.

### Example

```bash
~/skills/context/telegram/stage_a \
  --chat-id 567113516 \
  --from-id 567113516 \
  --kw 'retrieve-memory|telegram-context' \
```

### Output

- Writes intermediate files into `--out` (default `/tmp/tg_ctx/`):
  - `by_chat.txt`, `by_user.txt`, `by_kw.txt`
- Prints a de-duped, id-sorted candidate list on stdout (1 line per memory id).

Columns (TSV):
`id`, `routes`, `core_json`, `matched_detailed_line`

Notes:
- `routes` is a comma-separated list of which routes matched (`chat`, `user`, `kw`).
- `matched_detailed_line` is only present when `--kw` is provided (the `kw` route).

## Follow-ups (manual)

Once you have candidate record paths/IDs:
- Open a candidate `.core.json` to read metadata + `compacted` (one line).
- Open the sibling `.detailed.jsonl` to see the raw `input` (line 1), record
  `output` (line 2), and per-response tool calls (line 3+, one JSON array per line).
  This file can be verbose; prefer reading just the first few lines
  instead of loading the whole file.
