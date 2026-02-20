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
~/.kapybara/skills/context/telegram/stage_a \
  --chat-id <chat_id> \
  [--from-id <from_id>] \
  [--kw <regex>] \
  [--root <dir>] \
  [--n <N>] \
  --out <file>
```

Notes:
- The script records ripgrep match line numbers so you can jump to the exact `.detailed.jsonl` line.
- `--n` controls how many lines are kept per route (chat / user / kw). Default: `6`.

### Example

```bash
~/.kapybara/skills/context/telegram/stage_a \
  --chat-id 567113516 \
  --from-id 567113516 \
  --kw 'retrieve-memory|telegram-context' \
  --out /tmp/tg_ctx_<unique>.tsv \
```

### Output

- Writes the full output to `--out` (required) and also prints it to stdout.
  - Use a unique `/tmp/tg_ctx_<unique>.tsv` to avoid races/clobbering.
  - If `--out` is missing or already exists, the script errors and tells you to pick a unique path.

Columns (TSV):
`id`, `routes`, `core_json`, `matched_detailed_lines`

Notes:
- `routes` is a comma-separated list of which routes matched (`chat`, `user`, `kw`).
- `matched_detailed_lines` is a JSON array of `{line, text}` objects; it is only non-empty when `--kw` is provided.

## Follow-ups (manual)

Once you have candidate record paths/IDs:
- Open a candidate `.core.json` to read metadata + `compacted` (one line).
- Open the sibling `.detailed.jsonl` to see the raw `input` (line 1), record
  `output` (line 2), and per-response tool calls (line 3+, one JSON array per line).
  This file can be verbose; prefer reading just the first few lines
  instead of loading the whole file.
